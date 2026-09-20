"""
ICC Cloud Engine — Build 3 (multi-trade focus + re-signal logic)
Scans 6 symbols, matches setups against RAW historical outcomes including
their PATH (peak, giveback, bars-to-peak), and manages up to TWO focus
trades with history+momentum thresholds blended over fixed safety floors.
A+ setups during focus arrive as full cards: SWAP / TAKE BOTH / NOT TAKEN.
Same-symbol re-signals become TP-raise / CLOSE-&-SWAP / REVERSAL cards.
All execution is manual: the engine scans, decides, tracks, advises.
"""
import os, json, time, math, sqlite3
import urllib.request
from datetime import datetime, timezone
import config

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "memory.db")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

# ---------------- http ----------------
def http_json(url, payload=None, headers=None, timeout=15):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# ---------------- telegram ----------------
def tg(method, payload):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if tok.lower().startswith("bot"):
        tok = tok[3:].strip()
    if not tok:
        return None
    return http_json(config.TG_API.format(tok, method), payload)

def tg_send(text, buttons=None):
    p = {"chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""), "text": text, "parse_mode": "HTML"}
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    try:
        tg("sendMessage", p)
    except Exception as e:
        print("[tg] send failed:", e)

def tg_get_updates(offset):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if tok.lower().startswith("bot"):
        tok = tok[3:].strip()
    if not tok:
        return []
    try:
        resp = http_json(config.TG_API.format(tok, "getUpdates") + "?timeout=0", headers=UA)
    except Exception as e:
        print("[tg] getUpdates failed:", e)
        return []
    updates = resp.get("result", []) if isinstance(resp, dict) else []
    if not updates:
        return []
    ids = [int(u["update_id"]) for u in updates if isinstance(u, dict) and "update_id" in u]
    if not ids:
        return []
    stored = int(offset or 0)
    newest = max(ids)
    if stored > newest and stored - newest > 1000:
        print("[tg] old offset detected; resetting locally")
        stored = 0
    return [u for u in updates if int(u.get("update_id", -1)) >= stored]

# ---------------- indicators ----------------
def ema(vals, n):
    if not vals:
        return 0.0
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e

def atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    trs = trs[-n:]
    return sum(trs) / len(trs) if trs else 0.0

def rsi(closes, n=14):
    if len(closes) < n + 1:
        return 50.0
    g = sum(max(closes[i] - closes[i - 1], 0) for i in range(-n, 0))
    l = sum(max(closes[i - 1] - closes[i], 0) for i in range(-n, 0))
    if l == 0:
        return 100.0
    rs = (g / n) / (l / n)
    return 100 - 100 / (1 + rs)

def mom_band(m):
    return 0 if m < 35 else (1 if m < 65 else 2)

# ---------------- data ----------------
def fetch_chart(code, interval, rng, tries=3):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}?interval={interval}&range={rng}"
    err = None
    for i in range(tries):
        try:
            j = http_json(url, headers=UA)
            res = j["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            out = []
            for k, t in enumerate(res["timestamp"]):
                o, h, l, c = q["open"][k], q["high"][k], q["low"][k], q["close"][k]
                if None in (o, h, l, c):
                    continue
                out.append((float(t), float(o), float(h), float(l), float(c)))
            return out
        except Exception as e:
            err = e
            time.sleep(1.5 * (i + 1))
    print(f"[data] {code} {interval} failed: {err}")
    return []

def tf_state(bars):
    closes = [b[4] for b in bars]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    c = closes[-1]
    return {"close": c, "e20": e20, "e50": e50, "atr": atr(bars),
            "bull": c > e20 > e50, "bear": c < e20 < e50, "rsi": rsi(closes)}

def session_tag(ts):
    h = datetime.fromtimestamp(ts, timezone.utc).hour
    if 7 <= h < 12:
        return "LDN"
    if 12 <= h < 17:
        return "NY"
    if h < 6:
        return "ASIA"
    return "OFF"

# ---------------- db / state ----------------
def db_connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS memory(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        config_version TEXT, symbol TEXT, direction INTEGER, tf TEXT,
        disp REAL, cont REAL, depth REAL, sweep REAL, vol REAL,
        quality REAL, momentum REAL, regime INTEGER, session TEXT,
        decision INTEGER, entry REAL, sl REAL, tp REAL,
        outcome_type TEXT, r_result REAL, mfe REAL, mae REAL,
        opened_ts INTEGER, closed_ts INTEGER)""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(memory)").fetchall()]
    for name, typ in [("peak_r", "REAL"), ("failure_r", "REAL"), ("bars_peak", "INTEGER"),
                      ("bars_total", "INTEGER"), ("mfe_early", "REAL"), ("outcome_source", "TEXT")]:
        if name not in cols:
            con.execute(f"ALTER TABLE memory ADD COLUMN {name} {typ}")
    con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    return con

def load_state(con):
    row = con.execute("SELECT v FROM kv WHERE k='state'").fetchone()
    return json.loads(row[0]) if row else {}

def save_state(con, state):
    con.execute("INSERT INTO kv(k,v) VALUES('state',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (json.dumps(state),))

def remember(con, cand, decision, outcome_type, r_result, mfe, mae, opened_ts, path=None):
    path = path or {}
    con.execute("""INSERT INTO memory(config_version,symbol,direction,tf,disp,cont,depth,sweep,vol,
        quality,momentum,regime,session,decision,entry,sl,tp,outcome_type,r_result,mfe,mae,
        opened_ts,closed_ts,peak_r,failure_r,bars_peak,bars_total,mfe_early)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (config.CONFIG_VERSION, cand["symbol"], cand["direction"], cand.get("tf", "15m"),
         cand["feats"]["disp"], cand["feats"]["cont"], cand["feats"]["depth"],
         cand["feats"]["sweep"], cand["feats"]["vol"],
         cand.get("quality", 50), cand.get("momentum", 50), cand.get("regime", 0),
         cand.get("session", "?"), decision, cand["entry"], cand["sl"], cand.get("tp", 1.5),
         outcome_type, r_result, mfe, mae, opened_ts, int(time.time()),
         path.get("peak_r"), path.get("failure_r"), path.get("bars_peak"),
         path.get("bars_total"), path.get("mfe_early")))

# ---------------- memory matching (raw outcomes only) ----------------
def w_pct(pairs, q):
    if not pairs:
        return None
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= tot * q:
            return v
    return pairs[-1][0]

def match_memory(con, cand):
    rows = con.execute("""SELECT disp,cont,depth,sweep,vol,momentum,r_result,mfe,mae,
        opened_ts,regime,session,peak_r,failure_r,bars_peak
        FROM memory WHERE config_version=? AND symbol=? AND direction=? AND outcome_type='raw'""",
        (config.CONFIG_VERSION, cand["symbol"], cand["direction"])).fetchall()
    f = cand["feats"]
    band = mom_band(cand.get("momentum", 50))
    w_cnt = w_win = sum_r = best = 0.0
    peaks, gives, bars_p, maes = [], [], [], []
    for d, c, dep, sw, v, hm, res, mfe, mae, ts, reg, sess, pk, fail, bp in rows:
        if config.REQUIRE_REGIME_MATCH and reg != cand.get("regime", 0):
            continue
        if config.REQUIRE_SESSION_MATCH and sess != cand.get("session", "?"):
            continue
        s1 = max(0.0, 100 - abs(f["disp"] - d) * 50)
        s2 = max(0.0, 100 - abs(f["cont"] - c))
        s3 = max(0.0, 100 - abs(f["depth"] - dep) * 20)
        s4 = max(0.0, 100 - abs(f["sweep"] - sw))
        s5 = max(0.0, 100 - abs(f["vol"] - v))
        b = mom_band(hm)
        s6 = 100.0 if b == band else (60.0 if abs(b - band) == 1 else 0.0)
        sim = s1 * 0.21 + s2 * 0.21 + s3 * 0.17 + s4 * 0.13 + s5 * 0.13 + s6 * 0.15
        tier = 1.0 if sim >= config.STRICT_SIM else 0.7 if sim >= config.MODERATE_SIM else 0.4 if sim >= config.STRUCT_SIM else 0.0
        if tier <= 0:
            continue
        age_days = max(0.0, (time.time() - ts) / 86400)
        w = tier / math.pow(config.RECENT_WEIGHT, age_days / 30)
        w_cnt += w
        if res > 0:
            w_win += w
        sum_r += res * w
        best = max(best, sim)
        if pk is not None and pk > 0:
            peaks.append((pk, w))
            if fail is not None and pk > 0.3:
                gives.append((min(1.0, fail / pk), w))
            if bp is not None:
                bars_p.append((bp, w))
            if mae is not None:
                maes.append((abs(mae), w))
    exp = sum_r / w_cnt if w_cnt > 0 else 0.0
    tp = min(config.MAX_TP, max(config.MIN_TP, exp)) if w_cnt > 0 else config.TP_BASELINE_R
    behav = {"n_raw": len(peaks), "p50_peak": w_pct(peaks, 0.5), "p75_peak": w_pct(peaks, 0.75),
             "med_giveback": w_pct(gives, 0.5), "med_bars_peak": w_pct(bars_p, 0.5),
             "typ_mae": w_pct(maes, 0.5)}
    if behav["n_raw"] >= config.BEHAV_FULL_MATCHES and behav["p50_peak"]:
        tp = min(config.MAX_TP, max(config.MIN_TP, behav["p50_peak"] * 0.7 + behav["p75_peak"] * 0.3))
    behav["tp"] = tp
    return {"count": w_cnt, "wr": (w_win / w_cnt * 100) if w_cnt > 0 else 0.0,
            "exp": exp, "sim": best, "tp": tp, "behav": behav}

# ---------------- decision engine ----------------
def decide(cand, mem):
    q, m, conf = cand["quality"], cand["momentum"], cand["conf"]
    count, wr, exp = mem["count"], mem["wr"], mem["exp"]
    severe = q < config.REJECT_QUALITY or m < config.SEVERE or conf <= 1
    edge = (wr * 0.55 + q * 0.45) if count > 0 else q
    enough = count >= config.MIN_MATCHES
    weak_hist = enough and wr < config.SKIP_WR and exp < config.MIN_EXP
    strong_now = q >= config.RESCUE_QUALITY and m >= 70 and conf >= 3
    normal = enough and wr >= config.TAKE_WR and exp >= config.MIN_EXP and wr >= 50 and cand["mtf_ok"] and not severe and q >= 55
    rescue = enough and strong_now and mem["sim"] >= config.STRUCT_SIM and exp >= -0.25 and not severe
    take = (normal or rescue) and not severe
    reason = "HIST+CURRENT" if normal else ("CURRENT RESCUE" if rescue else "")
    if take and count < config.TAKE_MIN_MATCHES:
        return 2, f"WEAK EVIDENCE — needs {config.TAKE_MIN_MATCHES}+ matches", edge
    if take:
        return 1, reason or "HIST+CURRENT", edge
    if severe:
        return 3, ("MTF CONFLICT" if conf <= 1 else "CURRENT DETERIORATION"), edge
    if weak_hist and not strong_now:
        return 3, "WEAK HISTORY", edge
    if edge < config.SKIP_WR:
        return 3, "MIXED EDGE", edge
    return 2, "MIXED / WATCH", edge

# ---------------- analysis ----------------
def analyse_symbol(sym):
    cfg = config.SYMBOLS[sym]
    m15 = fetch_chart(cfg["yahoo"], "15m", "5d")
    h1 = fetch_chart(cfg["yahoo"], "60m", "1mo")
    d1 = fetch_chart(cfg["yahoo"], "1d", "3mo")
    if len(m15) < 60 or len(h1) < 30 or len(d1) < 20:
        return None
    s15, s60, sd = tf_state(m15), tf_state(h1), tf_state(d1)
    a = max(s15["atr"], 1e-9)
    lows = [b[3] for b in m15]
    highs = [b[2] for b in m15]
    swing_lo, swing_hi = min(lows[-30:-4]), max(highs[-30:-4])
    o, h, l, c = m15[-1][1], m15[-1][2], m15[-1][3], m15[-1][4]
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    bodyq = min(100.0, body / rng * 100)
    disp = min(5.0, body / a)
    bull_sweep = l < swing_lo and c > swing_lo
    bear_sweep = h > swing_hi and c < swing_hi
    an = {"symbol": sym, "candidate": None, "price": c, "o": o, "h": h, "l": l,
          "s15": s15, "s60": s60, "sd": sd, "bars15": m15, "atr15": a}
    up, dn = s15["bull"], s15["bear"]
    if up and s15["rsi"] > 50 and (bull_sweep or c > s15["e20"]) and c > o and bodyq >= 35:
        dirn = 1
    elif dn and s15["rsi"] < 50 and (bear_sweep or c < s15["e20"]) and c < o and bodyq >= 35:
        dirn = -1
    else:
        return an
    conf = sum([sd["bull"] if dirn == 1 else sd["bear"],
                s60["bull"] if dirn == 1 else s60["bear"],
                (s60["rsi"] > 50) if dirn == 1 else (s60["rsi"] < 50),
                up if dirn == 1 else dn])
    depth = min(5.0, (c - swing_lo) / a) if dirn == 1 else min(5.0, (swing_hi - c) / a)
    close_loc = (c - l) / rng * 100
    dl = close_loc if dirn == 1 else 100 - close_loc
    mom = (30 if up else 0) + (30 if (s60["bull"] if dirn == 1 else s60["bear"]) else 0) \
        + dl / 100 * 20 + min(20.0, disp / 1.5 * 20)
    liq = 10 if (bull_sweep if dirn == 1 else bear_sweep) else (6 if (c > s15["e20"] if dirn == 1 else c < s15["e20"]) else 2)
    rsi_q = min(10.0, max(0.0, (s15["rsi"] - 50) * 0.5)) if dirn == 1 else min(10.0, max(0.0, (50 - s15["rsi"]) * 0.5))
    trend_q = 20 if up else 0
    mtf_q = conf / 4 * 25
    quality = (trend_q + rsi_q + dl / 100 * 8 + bodyq / 100 * 7 + min(10.0, disp / 1.5 * 10) + 5.0 + liq + mtf_q) * 0.75 + mom * 0.25
    sl = (swing_lo - a * config.SL_BUFFER_ATR) if dirn == 1 else (swing_hi + a * config.SL_BUFFER_ATR)
    cand = {"symbol": sym, "tf": "15m", "direction": dirn, "entry": c, "sl": sl, "tp": 1.5,
            "feats": {"disp": disp, "cont": 1.0 if up else 0.0, "depth": depth,
                      "sweep": 1.0 if (bull_sweep if dirn == 1 else bear_sweep) else 0.0,
                      "vol": 50.0},
            "quality": quality, "momentum": mom, "conf": conf, "mtf_ok": conf >= 3,
            "regime": 1 if (s60["bull"] or s60["bear"]) else 0,
            "session": session_tag(m15[-1][0]), "price": c}
    an["candidate"] = cand
    return an

# ---------------- alerts / focus mode ----------------
def tp_price_from(f):
    return f["entry"] + (1 if f["direction"] == 1 else -1) * f["risk"] * f["tp_r"]

def blend(fallback, learned, w):
    if learned is None:
        return fallback
    return fallback * (1 - w) + learned * w

def send_setup_alert(state, cand, mem, decision, reason, edge, tag=""):
    d = "BUY" if cand["direction"] == 1 else "SELL"
    icon = "🟢" if decision == 1 else "🟠"
    tier = "TAKE" if decision == 1 else "RISKY"
    name = config.SYMBOLS[cand["symbol"]]["name"]
    tp_px = cand["entry"] + (1 if cand["direction"] == 1 else -1) * abs(cand["entry"] - cand["sl"]) * cand["tp"]
    b = mem["behav"]
    hist_line = ""
    if b["n_raw"] >= config.BEHAV_SOFT_MATCHES and b["p50_peak"]:
        hist_line = (f"\nBehaviour (raw n={b['n_raw']}): typical peak {b['p50_peak']:.2f}R"
                     + (f" | strong runs {b['p75_peak']:.2f}R" if b["p75_peak"] else "")
                     + (f" | giveback ~{b['med_giveback'] * 100:.0f}%" if b["med_giveback"] else ""))
    txt = (f"{icon} <b>{tier} — {name} {d}</b> {tag}\n"
           f"Entry {cand['entry']:.2f} | SL {cand['sl']:.2f} | TP {tp_px:.2f} ({cand['tp']:.2f}R)\n"
           f"Memory {mem['count']:.0f} | WR {mem['wr']:.0f}% | EXP {mem['exp']:+.2f}R | SIM {mem['sim']:.0f}%"
           + hist_line +
           f"\nQ {cand['quality']:.0f} | MOM {cand['momentum']:.0f} | MTF {cand['conf']}/4 | {cand['session']} reg={cand['regime']}\n"
           f"Reason: {reason} | Edge {edge:.0f} | v{config.CONFIG_VERSION}")
    tg_send(txt, [[{"text": "✅ TAKEN (TAKE)", "callback_data": "T|take"},
                   {"text": "⚠️ TAKEN (RISKY)", "callback_data": "T|risky"}],
                  [{"text": "✖ NOT TAKEN", "callback_data": "T|no"}]])

def corr_blocked(sym_a, sym_b):
    return any(sym_a in cl and sym_b in cl for cl in config.CLUSTERS)

def a_plus_check(cand, mem, state):
    if mem["count"] < 30 or mem["exp"] < 0.25 or cand["quality"] < 80 or cand["conf"] < 4:
        return False
    for t in state.get("focus_list", []):
        if t["symbol"] == cand["symbol"] and t["direction"] == cand["direction"]:
            return "DUP"
        if t["symbol"] == cand["symbol"]:
            return "RESIGNAL"
    return "OK"


def _pending_from(cand, mem, mode, tier="TAKE"):
    p = {k: cand[k] for k in ("symbol", "tf", "direction", "entry", "sl", "tp",
                              "feats", "quality", "momentum", "conf", "regime", "session")}
    p["tier"] = tier
    p["behav"] = mem["behav"]
    p["mode"] = mode
    return p


def aplus_focus_card(state, cand, mem, reason, edge, decision):
    name = config.SYMBOLS[cand["symbol"]]["name"]
    d = "BUY" if cand["direction"] == 1 else "SELL"
    tier = "TAKE" if decision == 1 else "RISKY"
    tp_px = cand["entry"] + (1 if cand["direction"] == 1 else -1) * abs(cand["entry"] - cand["sl"]) * cand["tp"]
    b = mem["behav"]
    hist_line = ""
    if b["n_raw"] >= config.BEHAV_SOFT_MATCHES and b["p50_peak"]:
        hist_line = (f"\nBehaviour (raw n={b['n_raw']}): typical peak {b['p50_peak']:.2f}R"
                     + (f" | strong runs {b['p75_peak']:.2f}R" if b["p75_peak"] else "")
                     + (f" | giveback ~{b['med_giveback'] * 100:.0f}%" if b["med_giveback"] else ""))
    fl = state.get("focus_list", [])
    open_lines = " | ".join(
        f"{config.SYMBOLS[t['symbol']]['name']} {'BUY' if t['direction'] == 1 else 'SELL'} {t.get('last_r', 0.0):+.2f}R"
        for t in fl) or "none"
    corr = any(corr_blocked(t["symbol"], cand["symbol"]) for t in fl)
    warn = "⚠️ CORRELATED with your open trade — they move together\n" if corr else ""
    total = 0.5 * (len(fl) + 1)
    txt = (f"🟡 A+ SETUP DURING FOCUS — YOUR CALL\n"
           f"Open: {open_lines}\n\n"
           f"New: {name} {d} ({tier})\n"
           f"Entry {cand['entry']:.2f} | SL {cand['sl']:.2f} | TP {tp_px:.2f} ({cand['tp']:.2f}R)\n"
           f"Memory {mem['count']:.0f} | WR {mem['wr']:.0f}% | EXP {mem['exp']:+.2f}R | SIM {mem['sim']:.0f}%"
           + hist_line +
           f"\nQ {cand['quality']:.0f} | MOM {cand['momentum']:.0f} | MTF {cand['conf']}/4\n"
           f"Reason: {reason} | Edge {edge:.0f}\n"
           f"{warn}If all stop out: ~-{total:.2f}% of account | Slots after: {len(fl) + 1}/2")
    state["pending"] = _pending_from(cand, mem, "aplus", tier)
    if len(fl) >= 2:
        tg_send(txt + "\n\n❌ Slots full (2/2) — /close a trade first if you want this one.",
                [[{"text": "✖ NOT TAKEN", "callback_data": "T|no"}]])
        return
    tg_send(txt,
            [[{"text": "✅ TAKEN — SWAP", "callback_data": "T|swap"},
              {"text": "➕ TAKE BOTH", "callback_data": "T|both"}],
             [{"text": "✖ NOT TAKEN", "callback_data": "T|no"}]])


def same_symbol_card(state, cand, mem, an):
    fl = state.get("focus_list", [])
    t = next((x for x in fl if x["symbol"] == cand["symbol"]), None)
    if not t:
        return
    name = config.SYMBOLS[cand["symbol"]]["name"]
    d = "BUY" if cand["direction"] == 1 else "SELL"
    od = "BUY" if t["direction"] == 1 else "SELL"
    atr15 = max(an.get("atr15", 0.0), 1e-9)
    mom = t.get("momentum", 50.0)
    r = t.get("last_r", 0.0)
    healthy = mom >= 45 and r > -0.25
    better = cand["quality"] >= t.get("quality", 50) + 8 or cand["quality"] >= 80
    if cand["direction"] != t["direction"]:
        if cand["momentum"] >= 70 and cand["conf"] >= 3:
            txt = (f"⚠️ REVERSAL WARNING — {name}\n"
                   f"Open: {od} | R {r:+.2f} | momentum {mom:.0f}\n"
                   f"New: strong {d} signal (Q {cand['quality']:.0f}, MOM {cand['momentum']:.0f}, MTF {cand['conf']}/4)\n"
                   f"Your call: exit and reverse, or hold through.")
            state["pending"] = _pending_from(cand, mem, "REVERSAL")
            tg_send(txt, [[{"text": "🔁 CLOSE & SWAP", "callback_data": "T|swap"},
                           {"text": "✖ KEEP CURRENT", "callback_data": "T|no"}]])
        return
    same_setup = abs(cand["entry"] - t["entry"]) <= 0.75 * atr15
    if same_setup and healthy and cand["tp"] >= t["tp_r"] + 0.15 \
            and cand["momentum"] >= 70 and cand["conf"] >= 3:
        newtp = min(config.MAX_TP, cand["tp"])
        newpx = t["entry"] + t["direction"] * t["risk"] * newtp
        txt = (f"🔁 SAME-SYMBOL RE-SIGNAL — {name} {od}\n"
               f"Open: R {r:+.2f} | momentum {mom:.0f}\n"
               f"Current TP {tp_price_from(t):.2f} ({t['tp_r']:.2f}R)\n"
               f"Supported TP {newpx:.2f} ({newtp:.2f}R) — fresh A+ signal + memory agree\n"
               f"Decision: RAISE TP (entry/risk unchanged)")
        state["pending"] = _pending_from(cand, mem, "TPUP")
        state["pending"]["new_tp_r"] = newtp
        tg_send(txt, [[{"text": "✅ APPLY TP", "callback_data": "T|tpup"},
                       {"text": "✖ HOLD CURRENT", "callback_data": "T|no"}]])
    elif not healthy and better:
        txt = (f"⚠️ {name} {od} LOST MOMENTUM\n"
               f"Open R: {r:+.2f} | momentum {mom:.0f}\n"
               f"New setup: quality {cand['quality']:.0f} | direction {d}\n"
               f"Decision: CLOSE CURRENT & SWAP")
        state["pending"] = _pending_from(cand, mem, "SWAP")
        tg_send(txt, [[{"text": "🔁 CLOSE & SWAP", "callback_data": "T|cswap"},
                       {"text": "✖ KEEP CURRENT", "callback_data": "T|no"}]])
    elif same_setup and healthy:
        tg_send(f"ℹ️ {name} re-signalled the same setup, but TP-raise conditions not met — holding current management.")
    else:
        tg_send(f"ℹ️ {name} new structure detected while trade healthy — holding current (no swap).")

def consider_candidates(con, state, analyses):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"], state["daily_r"] = today, 0.0
    if time.time() < state.get("pause_until", 0):
        print("[risk] loss-streak pause active — no new alerts")
        return
    if state.get("daily_r", 0.0) <= config.DAILY_LOSS_R:
        print("[risk] daily loss limit hit — no new alerts today")
        return
    cands = []
    for sym, an in analyses.items():
        if an and an["candidate"]:
            mem = match_memory(con, an["candidate"])
            an["candidate"]["tp"] = mem["tp"]
            cands.append((an["candidate"], mem))
    if not cands:
        return
    focus_list = state.get("focus_list", [])
    ranked = sorted(cands, key=lambda cm: (cm[1]["exp"] if cm[1]["count"] > 0 else -9.0, cm[0]["quality"]), reverse=True)
    cand, mem = ranked[0]
    key = f"{cand['symbol']}|{cand['direction']}"
    if time.time() < state.get("cooldowns", {}).get(key, 0):
        return
    decision, reason, edge = decide(cand, mem)
    if focus_list:
        ap = a_plus_check(cand, mem, state)
        if ap == "DUP":
            return
        if ap == "RESIGNAL":
            same_symbol_card(state, cand, mem, analyses[cand["symbol"]])
            state.setdefault("cooldowns", {})[key] = time.time() + config.COOLDOWN_H * 3600
            return
        if ap == "OK" and decision != 3:
            aplus_focus_card(state, cand, mem, reason, edge, decision)
            state.setdefault("cooldowns", {})[key] = time.time() + config.COOLDOWN_H * 3600
            return
        if decision != 3:
            return
    if decision == 3:
        rp = dict(cand)
        rp["decision"] = 3
        rp["risk"] = abs(cand["entry"] - cand["sl"])
        rp["tp_r"] = cand["tp"]
        rp["opened_ts"] = int(time.time())
        rp["last_ts"] = analyses[cand["symbol"]]["bars15"][-1][0]
        rp["path_peak"] = 0.0
        rp["path_trough"] = 0.0
        rp["path_giveback"] = 0.0
        rp["path_bars"] = 0
        state.setdefault("replays", []).append(rp)
        state["replays"] = state["replays"][-50:]
        print(f"[setup] {cand['symbol']} SKIP ({reason}) — queued for raw replay")
        return
    send_setup_alert(state, cand, mem, decision, reason, edge)
    cd = state.get("cooldowns", {})
    cd[key] = time.time() + config.COOLDOWN_H * 3600
    state["cooldowns"] = cd
    state["pending"] = _pending_from(cand, mem, "normal", "TAKE" if decision == 1 else "RISKY")
    print(f"[setup] {cand['symbol']} {decision} alerted ({reason})")

def manage_focus(state, analyses):
    fl = state.get("focus_list", [])
    if not fl:
        return
    for f in fl:
        an = analyses.get(f["symbol"])
        if not an:
            continue
        price, dirn = an["price"], f["direction"]
        r = (price - f["entry"]) / f["risk"] if dirn == 1 else (f["entry"] - price) / f["risk"]
        f["mfe"] = max(f.get("mfe", 0.0), r)
        f["mae"] = min(f.get("mae", 0.0), r)
        f["last_r"] = r
        b = f.get("behav", {})
        n_raw = b.get("n_raw", 0)
        w = 0.0 if n_raw < config.BEHAV_SOFT_MATCHES else min(1.0, (n_raw - 20) / 60.0) * 0.75
        p50 = b.get("p50_peak")
        p75 = b.get("p75_peak")
        med_gb = b.get("med_giveback")
        protect_r = blend(config.MIN_PROTECT_R, min(1.2, max(0.35, p50 * 0.75)) if p50 else None, w)
        ext_r = blend(1.0, min(2.0, p75 * 0.8) if p75 else None, w)
        mfe_gate = blend(1.0, p50 if p50 else None, w)
        gb_now = (f["mfe"] - r) / f["mfe"] if f["mfe"] > 0.2 else 0.0
        f["peak_r"] = max(f.get("peak_r", 0.0), r)
        if f["peak_r"] > 0.3:
            f["giveback"] = max(f.get("giveback", 0.0), (f["peak_r"] - r) / f["peak_r"])
        f["bars_held"] = f.get("bars_held", 0) + 1
        s15, s60 = an["s15"], an["s60"]
        htf_ok = s60["bull"] if dirn == 1 else s60["bear"]
        rng = max(an["h"] - an["l"], 1e-9)
        dl = (price - an["l"]) / rng * 100 if dirn == 1 else (an["h"] - price) / rng * 100
        disp_now = min(5.0, abs(price - an["o"]) / max(an["atr15"], 1e-9))
        mom = (30 if (s15["bull"] if dirn == 1 else s15["bear"]) else 0) + (30 if htf_ok else 0) \
            + dl / 100 * 20 + min(20.0, disp_now / 1.5 * 20)
        f["momentum"] = mom
        msgs = []
        if not f.get("be_done") and r >= protect_r and (mom < config.WEAK or f["mfe"] >= mfe_gate):
            be = f["entry"] + dirn * f["risk"] * config.BE_OFFSET_R
            src = "history" if w > 0.3 else "default"
            msgs.append(f"🛡 MOVE SL → BREAK-EVEN @ {be:.2f} (protect @{protect_r:.2f}R, {src})")
            f["be_done"] = True
        if not f.get("reduced") and r > 0.2 and mom < config.WEAK and not htf_ok \
                and (n_raw < config.BEHAV_FULL_MATCHES or med_gb is None or gb_now >= med_gb):
            newtp = max(config.MIN_TP, min(config.MAX_TP, f["mfe"] * 0.8))
            f["tp_r"] = min(f["tp_r"], newtp)
            msgs.append(f"✂ REDUCE TP → {tp_price_from(f):.2f} ({f['tp_r']:.2f}R) — weak momentum, giving back like similar setups")
            f["reduced"] = True
        if not f.get("extended") and r >= ext_r and htf_ok and mom >= 70:
            target = min(config.MAX_TP, max(f["tp_r"], p75 if (p75 and w > 0.3) else f["tp_r"] * 1.2))
            f["tp_r"] = target
            f["extended"] = True
            msgs.append(f"🚀 EXTEND TP → {tp_price_from(f):.2f} ({target:.2f}R) — 1h confirms; strong runs reach {p75:.2f}R" if p75 else
                        f"🚀 EXTEND TP → {tp_price_from(f):.2f} ({target:.2f}R) — 1h confirms the move")
        if f.get("be_done") and mom < config.SEVERE and \
                (n_raw < config.BEHAV_FULL_MATCHES or med_gb is None or gb_now >= med_gb * 1.5 or r > 0.15):
            msgs.append("🚨 EXIT ZONE — momentum gone after protection. Consider manual exit.")
        sl_hit = price <= f["sl"] if dirn == 1 else price >= f["sl"]
        tp_hit = price >= tp_price_from(f) if dirn == 1 else price <= tp_price_from(f)
        if sl_hit:
            msgs.append("⚠️ Price at/beyond your SL level — confirm with /close <R>")
        if tp_hit:
            msgs.append("🎯 Price at/near TP — /close to log it")
        name = config.SYMBOLS[f["symbol"]]["name"]
        dname = "BUY" if dirn == 1 else "SELL"
        if msgs:
            head = f"⚙️ MANAGING {name} {dname} ({f['tier']}) | R {r:+.2f}\n"
            tg_send(head + "\n".join(msgs),
                    [[{"text": f"🏁 CLOSED — log {f['symbol']}", "callback_data": f"C|close:{f['symbol']}"}]])
            f["pings"] = 0
        else:
            f["pings"] = f.get("pings", 0) + 1
            if len(fl) == 1 and f["pings"] >= 8:
                f["pings"] = 0
                tg_send(f"⏳ POSITION OPEN — SYNCING | {name} {dname} | R {r:+.2f} | MFE {f['mfe']:+.2f} | peak {f.get('peak_r', 0):.2f}",
                        [[{"text": f"🏁 CLOSED — log {f['symbol']}", "callback_data": f"C|close:{f['symbol']}"}]])
    if len(fl) > 1 and all(t.get("pings", 0) >= 8 for t in fl):
        for t in fl:
            t["pings"] = 0
        summary = " | ".join(f"{config.SYMBOLS[t['symbol']]['name']} {'BUY' if t['direction'] == 1 else 'SELL'} {t.get('last_r', 0.0):+.2f}R" for t in fl)
        comb = sum(t.get("last_r", 0.0) for t in fl)
        tg_send(f"📊 FOCUS {len(fl)}/2 — {summary}\nCombined floating: {comb:+.2f}R")

def close_focus(con, state, r_override=None, sym=None):
    fl = state.get("focus_list", [])
    if not fl:
        return "no open trade"
    if sym:
        f = next((x for x in fl if x["symbol"] == sym), None)
        if not f:
            return f"no open trade on {sym}"
    else:
        if len(fl) > 1:
            names = ", ".join(config.SYMBOLS[t["symbol"]]["name"] for t in fl)
            return f"multiple open ({names}) — use /close SYMBOL <R>"
        f = fl[0]
    r = r_override if r_override is not None else f.get("last_r", 0.0)
    decision = 1 if f["tier"] == "TAKE" else 2
    cand = {"symbol": f["symbol"], "tf": f.get("tf", "15m"), "direction": f["direction"],
            "entry": f["entry"], "sl": f["sl"], "tp": f["tp_r"], "feats": f["feats"],
            "quality": f.get("quality", 50), "momentum": f.get("momentum", 50),
            "conf": f.get("conf", 2), "regime": f.get("regime", 0), "session": f.get("session", "?")}
    pk = f.get("peak_r", max(f.get("mfe", 0.0), r))
    path = {"peak_r": round(pk, 3),
            "failure_r": round(max(0.0, pk - r), 3),
            "bars_peak": None, "bars_total": f.get("bars_held"),
            "mfe_early": f.get("mfe", 0.0)}
    remember(con, cand, decision, "managed", r, f.get("mfe", 0.0), f.get("mae", 0.0), f["opened_ts"], path)
    state["loss_streak"] = (state.get("loss_streak", 0) + 1) if r < 0 else 0
    if state["loss_streak"] >= config.MAX_LOSS_STREAK:
        state["pause_until"] = time.time() + config.PAUSE_SCANS * config.CADENCE_MIN * 60
        tg_send(f"🛑 {config.MAX_LOSS_STREAK} losses in a row — engine paused for {config.PAUSE_SCANS} scans.")
    state["daily_r"] = state.get("daily_r", 0.0) + r
    state["focus_list"] = [x for x in fl if x is not f]
    return f"logged {r:+.2f}R (managed, {f['tier']})"

# ---------------- telegram updates ----------------
def start_focus_trade(state, p, tier=None):
    if tier:
        p = dict(p)
        p["tier"] = tier
    t = {"symbol": p["symbol"], "tf": p["tf"], "direction": p["direction"],
         "tier": p["tier"], "entry": p["entry"], "sl": p["sl"],
         "risk": abs(p["entry"] - p["sl"]), "tp_r": p["tp"], "feats": p["feats"],
         "quality": p["quality"], "momentum": p["momentum"], "conf": p["conf"],
         "regime": p["regime"], "session": p["session"], "behav": p.get("behav", {}),
         "opened_ts": int(time.time()), "mfe": 0.0, "mae": 0.0,
         "peak_r": 0.0, "giveback": 0.0, "bars_held": 0,
         "be_done": False, "reduced": False, "extended": False, "pings": 0}
    fl = state.setdefault("focus_list", [])
    if any(x["symbol"] == t["symbol"] and x["direction"] == t["direction"] for x in fl):
        tg_send(f"Already tracking {config.SYMBOLS[t['symbol']]['name']} that direction — not duplicated.")
        return
    if len(fl) >= 2:
        tg_send("Slots full (2/2) — /close a trade first.")
        return
    fl.append(t)
    name = config.SYMBOLS[t["symbol"]]["name"]
    d = "BUY" if t["direction"] == 1 else "SELL"
    tg_send(f"🔒 TRACKING {name} {d} ({t['tier']}) — open trades {len(fl)}/2\n"
            f"Entry {t['entry']:.2f} | SL {t['sl']:.2f} | TP {tp_price_from(t):.2f}\n"
            f"Management = history + momentum.",
            [[{"text": f"🏁 CLOSED — log {t['symbol']}", "callback_data": f"C|close:{t['symbol']}"}]])


def handle_ack(con, state, data):
    p = state.get("pending")
    if data in ("T|take", "T|risky"):
        if not p:
            tg_send("No pending setup to acknowledge.")
            return
        start_focus_trade(state, p, "TAKE" if data == "T|take" else "RISKY")
        state["pending"] = None
    elif data == "T|both":
        if not p or p.get("mode") != "aplus":
            tg_send("No A+ card pending.")
            return
        start_focus_trade(state, p)
        state["pending"] = None
    elif data == "T|swap":
        if not p or p.get("mode") not in ("aplus", "REVERSAL"):
            tg_send("No swap card pending.")
            return
        fl = state.get("focus_list", [])
        if len(fl) > 1:
            tg_send("Two trades open — /close SYMBOL first, then tap swap again.")
            return
        if fl:
            tg_send(f"🏁 {close_focus(con, state, None)}")
        start_focus_trade(state, p)
        state["pending"] = None
    elif data == "T|cswap":
        if not p or p.get("mode") != "SWAP":
            tg_send("No swap card pending.")
            return
        tg_send(f"🏁 {close_focus(con, state, None)}")
        start_focus_trade(state, p)
        state["pending"] = None
    elif data == "T|tpup":
        if not p or p.get("mode") != "TPUP":
            tg_send("No TP card pending.")
            return
        t = next((x for x in state.get("focus_list", []) if x["symbol"] == p["symbol"]), None)
        if t:
            t["tp_r"] = p["new_tp_r"]
            tg_send(f"✅ TP raised → {tp_price_from(t):.2f} ({t['tp_r']:.2f}R). Entry/risk unchanged — update TP in your broker app.")
        state["pending"] = None
    elif data == "T|no":
        state["pending"] = None
        tg_send("Noted — not taken. Similar setups stay quiet for the cooldown period.")
    elif data.startswith("C|close"):
        sym = data.split(":", 1)[1] if ":" in data else None
        tg_send(f"🏁 {close_focus(con, state, None, sym)} | Open trades: {len(state.get('focus_list', []))}/2")

def handle_command(con, state, txt):
    if txt.startswith("/taken"):
        parts = txt.split()
        if len(parts) >= 6:
            sym = parts[1].upper()
            d = 1 if parts[2].upper().startswith("B") else -1
            try:
                entry, sl, tp = float(parts[3]), float(parts[4]), float(parts[5])
            except ValueError:
                tg_send("Format: /taken XAUUSD BUY 2650.5 2645.0 2660.0 RISKY")
                return
            if sym not in config.SYMBOLS:
                tg_send(f"Unknown symbol. Use: {', '.join(config.SYMBOLS)}")
                return
            tier = parts[6].upper() if len(parts) > 6 else "MANUAL"
            p = {"symbol": sym, "tf": "manual", "direction": d, "tier": tier,
                 "entry": entry, "sl": sl, "tp": abs(tp - entry) / max(abs(entry - sl), 1e-9),
                 "feats": {"disp": 0, "cont": 0, "depth": 0, "sweep": 0, "vol": 50},
                 "quality": 50, "momentum": 50, "conf": 2, "regime": 0,
                 "session": session_tag(time.time()), "behav": {}}
            start_focus_trade(state, p)
        else:
            tg_send("Format: /taken XAUUSD BUY 2650.5 2645.0 2660.0 RISKY")
    elif txt.startswith("/close"):
        parts = txt.split()
        sym, r_override = None, None
        rest = parts[1:]
        if rest and rest[0].upper() in config.SYMBOLS:
            sym = rest[0].upper()
            rest = rest[1:]
        if rest:
            try:
                r_override = float(rest[0])
            except ValueError:
                pass
        tg_send(f"🏁 {close_focus(con, state, r_override, sym)} | Open trades: {len(state.get('focus_list', []))}/2")
    elif txt == "/status":
        fl = state.get("focus_list", [])
        if fl:
            ftxt = " | ".join(
                f"{config.SYMBOLS[t['symbol']]['name']} {'BUY' if t['direction'] == 1 else 'SELL'} {t.get('last_r', 0.0):+.2f}R"
                for t in fl)
        else:
            ftxt = "none"
        tg_send(f"ICC {config.CONFIG_VERSION} | focus: {ftxt} ({len(fl)}/2) | "
                f"daily R {state.get('daily_r', 0):+.2f} | streak {state.get('loss_streak', 0)}")

def process_updates(con, state):
    ups = tg_get_updates(state.get("tg_offset", 0) + 1)
    for u in ups:
        state["tg_offset"] = max(state.get("tg_offset", 0), u.get("update_id", 0))
        if "callback_query" in u:
            cq = u["callback_query"]
            try:
                tg("answerCallbackQuery", {"callback_query_id": cq["id"]})
            except Exception:
                pass
            handle_ack(con, state, cq.get("data", ""))
        elif "message" in u:
            handle_command(con, state, (u["message"].get("text") or "").strip())

# ---------------- replay learning (path-aware) ----------------
def resolve_replays(con, state, analyses):
    ttl_s = config.REPLAY_TTL_H * 3600
    keep = []
    for rp in state.get("replays", []):
        an = analyses.get(rp["symbol"])
        if not an:
            keep.append(rp)
            continue
        dirn = rp["direction"]
        entry, sl, risk, tp_r = rp["entry"], rp["sl"], rp["risk"], rp["tp_r"]
        tp_px = entry + dirn * risk * tp_r
        new_bars = [b for b in an["bars15"] if b[0] > rp.get("last_ts", 0)]
        baseline_done = None
        for b in new_bars:
            ts, bh, bl, bc = b[0], b[2], b[3], b[4]
            r_hi = (bh - entry) / risk if dirn == 1 else (entry - bl) / risk
            r_lo = (bl - entry) / risk if dirn == 1 else (entry - bh) / risk
            rp["path_peak"] = round(max(rp.get("path_peak", 0.0), r_hi, r_lo), 3)
            rp["path_trough"] = round(min(rp.get("path_trough", 0.0), r_lo, r_hi), 3)
            pk = rp["path_peak"]
            if pk > 0.3:
                rp["path_giveback"] = round(max(rp.get("path_giveback", 0.0),
                                                (pk - min(r_lo, r_hi)) / pk), 3)
            rp["path_bars"] = rp.get("path_bars", 0) + 1
            sl_hit = bl <= sl if dirn == 1 else bh >= sl
            tp_hit = bh >= tp_px if dirn == 1 else bl <= tp_px
            if baseline_done is None:
                if sl_hit:
                    baseline_done = -1.0
                elif tp_hit:
                    baseline_done = tp_r
            rp["last_ts"] = ts
        elapsed = time.time() - rp["opened_ts"]
        if baseline_done is None and elapsed > ttl_s:
            baseline_done = round(((an["price"] - entry) / risk) if dirn == 1
                                  else ((entry - an["price"]) / risk), 3)
        if baseline_done is None and not new_bars:
            keep.append(rp)
            continue
        if baseline_done is None:
            keep.append(rp)
            continue
        path = {"peak_r": rp.get("path_peak"), "failure_r": rp.get("path_giveback"),
                "bars_peak": None, "bars_total": rp.get("path_bars"), "mfe_early": None}
        remember(con, rp, rp.get("decision", 3), "raw", baseline_done,
                 rp.get("path_peak", 0.0), rp.get("path_trough", 0.0), rp["opened_ts"], path)
        print(f"[replay] {rp['symbol']} resolved {baseline_done:+.2f}R | peak {rp.get('path_peak', 0):.2f} | giveback {rp.get('path_giveback', 0):.2f}")
    state["replays"] = keep

# ---------------- main ----------------
def main():
    con = db_connect()
    state = load_state(con)
    if not isinstance(state.get("focus_list"), list):
        old = state.pop("focus", None)
        state["focus_list"] = [old] if old else []
    n = con.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    print(f"[icc] engine {config.CONFIG_VERSION} | memory rows: {n}")
    process_updates(con, state)
    analyses = {}
    for sym in config.SYMBOLS:
        an = analyse_symbol(sym)
        if an:
            analyses[sym] = an
    print(f"[icc] analysed {len(analyses)}/{len(config.SYMBOLS)} symbols")
    manage_focus(state, analyses)
    consider_candidates(con, state, analyses)
    resolve_replays(con, state, analyses)
    n2 = con.execute("SELECT COUNT(*) FROM memory WHERE config_version=?", (config.CONFIG_VERSION,)).fetchone()[0]
    if n2 > config.MAX_HISTORY:
        con.execute("DELETE FROM memory WHERE id IN (SELECT id FROM memory WHERE config_version=? ORDER BY id LIMIT ?)",
                    (config.CONFIG_VERSION, n2 - config.MAX_HISTORY))
    save_state(con, state)
    con.commit()
    con.close()
    print("[icc] scan complete")

if __name__ == "__main__":
    main()
