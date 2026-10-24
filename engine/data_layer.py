"""data_layer — OpenTerminal-inspired provider chain for the ICC engine.

Ported from ErTasselli/OpenTerminal server/src/providers:
  yahoo.ts   → YahooProvider (v8 chart, UA pool, backoff on 429)
  stooq.ts   → StooqProvider (daily CSV fallback)
  binance.ts → BinanceProvider (klines, crypto fallback)
  fred.ts    → fred_vix / fred_series (VIX + yields, keyless CSV)
  registry.py → with_fallback() + per-provider health stats
Plus a small TTL cache (stale-while-revalidate idea from OpenTerminal cache).

Public contract kept identical to the old engine.fetch_chart:
  candles = fetch_candles(yahoo_code, interval, range)
  → list of (ts, open, high, low, close) tuples, newest last.
"""

import csv
import io
import json
import random
import threading
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------- UA pool
# One UA per process (sticky), like OpenTerminal's yahoo.ts.
UA_POOL = [
    "Mozilla/5.0 (X11; Linux x86_64; rv:141.0) Gecko/20100101 Firefox/141.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7; rv:140.0) Gecko/20100101 Firefox/140.0",
]
UA = random.choice(UA_POOL)

_lock = threading.Lock()

# ---------------------------------------------------------------- cache
# key → (fetched_at, value). Intraday TTL 5 min, daily TTL 6 h.
_cache = {}
_INTRADAY_TTL = 300
_DAILY_TTL = 21600


def _ttl_for(interval):
    return _DAILY_TTL if interval == "1d" else _INTRADAY_TTL


def _http(url, timeout=12, tries=2):
    """GET helper with UA header + retry. Returns decoded text."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001 — provider errors all become raises below
            last = e
            time.sleep(0.8 * (i + 1))
    raise RuntimeError(f"http failed {url}: {last}")


# ---------------------------------------------------------------- stats (registry.ts port)
_stats = {}


def _tracked(name, fn):
    s = _stats.setdefault(name, {"ok": 0, "failed": 0, "lastLatencyMs": None, "lastError": None})
    t0 = time.time()
    try:
        out = fn()
        s["ok"] += 1
        s["lastLatencyMs"] = int((time.time() - t0) * 1000)
        return out
    except Exception as e:
        s["failed"] += 1
        s["lastError"] = str(e)[:300]
        raise


def provider_stats():
    return dict(_stats)


def with_fallback(attempts):
    """attempts: [(name, callable)] → first success, else last error."""
    last = RuntimeError("no providers configured")
    for name, fn in attempts:
        try:
            return _tracked(name, fn)
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


# ---------------------------------------------------------------- Yahoo (primary)
def yahoo_history(code, interval, rng):
    """query1/query2 rotation with 429 backoff — both hosts mirror the same API."""
    last = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{code}"
               f"?interval={interval}&range={rng}")
        try:
            j = json.loads(_http(url, tries=2))
            break
        except Exception as e:  # noqa: BLE001
            last = e
            if "429" in str(e):
                time.sleep(3.0)  # polite backoff before the other host
    else:
        raise last  # both hosts failed
    res = j["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for k, t in enumerate(res.get("timestamp", [])):
        o, h, l, c = q["open"][k], q["high"][k], q["low"][k], q["close"][k]
        if None in (o, h, l, c):
            continue
        out.append((float(t), float(o), float(h), float(l), float(c)))
    if not out:
        raise RuntimeError(f"yahoo: no candles for {code}")
    return out


# ---------------------------------------------------------------- Stooq (daily CSV fallback)
def _stooq_symbol(yahoo_code):
    s = yahoo_code.lower()
    if s.startswith("^") or "=" in s or "." in s or "-" in s:
        return s
    return s + ".us"


def stooq_history(yahoo_code):
    """Daily candles only — Stooq has no intraday free feed."""
    url = f"https://stooq.com/q/d/l/?s={_stooq_symbol(yahoo_code)}&i=d"
    text = _http(url)
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2 or not rows[0] or not rows[0][0].lower().startswith("date"):
        raise RuntimeError("stooq: no data")
    out = []
    for row in rows[1:]:
        if len(row) < 6:
            continue
        date, o, h, l, c = row[0], row[1], row[2], row[3], row[4]
        try:
            ts = time.mktime(time.strptime(date, "%Y-%m-%d"))
            out.append((ts, float(o), float(h), float(l), float(c)))
        except ValueError:
            continue
    if not out:
        raise RuntimeError("stooq: empty parse")
    return out


# ---------------------------------------------------------------- Binance (crypto fallback)
_BINANCE_INTERVALS = {"15m": "15m", "60m": "1h", "1h": "1h", "1d": "1d"}


def binance_history(yahoo_code, interval, rng):
    """Only crypto symbols. Maps Yahoo BTC-USD → BTCUSDT klines."""
    if "BTC" not in yahoo_code.upper() and "ETH" not in yahoo_code.upper():
        raise RuntimeError("binance: symbol not crypto")
    pair = ("BTC" if "BTC" in yahoo_code.upper() else "ETH") + "USDT"
    bi = _BINANCE_INTERVALS.get(interval)
    if not bi:
        raise RuntimeError(f"binance: unsupported interval {interval}")
    # range → limit
    limit = {"5d": 200, "1mo": 300, "3mo": 220, "6mo": 180}.get(rng, 300)
    url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval={bi}&limit={limit}"
    rows = json.loads(_http(url))
    out = []
    for r in rows:
        out.append((r[0] / 1000.0, float(r[1]), float(r[2]), float(r[3]), float(r[4])))
    if not out:
        raise RuntimeError("binance: empty")
    return out


# ---------------------------------------------------------------- FRED (VIX / yields, keyless)
def fred_series(series_id, last_n=60):
    """Port of OpenTerminal fred.ts — public CSV, no API key."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    text = _http(url)
    lines = text.strip().split("\n")[1:]
    points = []
    for line in lines:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        date, raw = parts[0], parts[1].strip()
        try:
            v = float(raw)
        except ValueError:
            continue  # FRED uses "." for missing
        points.append((date, v))
    return points[-last_n:]


def fred_vix():
    pts = fred_series("VIXCLS", 30)
    if not pts:
        raise RuntimeError("fred: no VIX")
    return pts[-1][1]  # (date, value)


# ---------------------------------------------------------------- public fetch
_YAHOO_CODE_TO_BINANCE = ("BTC-USD", "ETH-USD")


def fetch_candles(code, interval, rng, tries=3):
    """Old fetch_chart contract: (code, interval, range) → candles, newest last.

    Fallback chain per OpenTerminal's registry.withFallback():
      intraday: Yahoo only (Stooq has no intraday, Binance only for crypto)
      daily:    Yahoo → Binance(crypto) → Stooq
    """
    key = f"{code}|{interval}|{rng}"
    ttl = _ttl_for(interval)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]

    last = None
    order = [("yahoo", lambda: yahoo_history(code, interval, rng))]
    if interval == "1d":
        if any(s in code.upper() for s in _YAHOO_CODE_TO_BINANCE):
            order.append(("binance", lambda: binance_history(code, interval, rng)))
        order.append(("stooq", lambda: stooq_history(code)))

    for name, fn in order:
        for attempt in range(tries):
            try:
                out = _tracked(name, fn)
                with _lock:
                    _cache[key] = (time.time(), out)
                return out
            except Exception as e:  # noqa: BLE001
                last = e
                if "429" in str(e):
                    time.sleep(2.0 * (attempt + 1))  # rate-limited: back off
                else:
                    break  # non-429 → next provider
        # provider exhausted → next in chain
    raise RuntimeError(f"all providers failed for {code} {interval}: {last}")


# Back-compat alias used by engine/pressure/backfill
fetch_chart = fetch_candles
