"""risk.py — independent risk firewall for the ICC engine.

Design rules (Phase 5F):
  * The firewall is INDEPENDENT of strategy output. Strategy decides what it
    wants; this module decides whether it is allowed to open. It can only ever
    REJECT — it never edits, improves or resizes a setup, so strategy behaviour
    is unchanged when all checks pass.
  * Every limit is configurable (config constants, overridable per deployment
    through RISK_* environment variables). No account size is assumed here.
  * FAIL CLOSED: if an input is missing or its age cannot be proven, the open
    is rejected with a reason. Uncertainty never becomes permission.
  * It cannot be bypassed: engine.start_focus_trade() is the single funnel for
    every open path (button acks, /taken, A+ swap, pressure cards).

Checks, in order:
  1. kill switch (file or state flag)
  2. loss-streak pause
  3. daily loss limit
  4. max concurrent positions
  5. duplicate position (same symbol + direction)
  6. conflicting/hedged position on the same symbol
  7. max opens per day
  8. pending-card age (stale signals are refused)
  9. market-data age (stale or unverifiable feed refused)
 10. stop-loss validity (present, on the correct side, non-zero risk)
 11. reward bounds (TP within configured R:R envelope)
 12. entry sanity

Manual entries (`p["source"] == "manual"`, produced only by the owner's
/taken command) carry no card and no feed bar: the position already exists at
the broker, so refusing to register it would leave it UNMANAGED. They are
validated for structural safety only (entry/stop validity, duplicates,
concurrency, daily cap); card-age, feed-age and the R:R envelope are skipped and
the audit record stores `source=manual`. Engine-generated setups always carry
created_at + bar_ts and get every check.
"""

import os
import time

import config


# ---------------------------------------------------------------- config glue
def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def limits():
    """Effective risk limits: config defaults, overridable per deployment."""
    return {
        "daily_loss_r": _env_float("RISK_DAILY_LOSS_R", config.DAILY_LOSS_R),
        "max_concurrent": _env_int("RISK_MAX_CONCURRENT", 2),
        "max_opens_day": _env_int("RISK_MAX_OPENS_DAY", 6),
        "pending_max_age_s": _env_int("RISK_PENDING_MAX_AGE_S", 1800),
        "bar_max_age_s": _env_int("RISK_BAR_MAX_AGE_S", 1800),
        "daily_bar_max_age_s": _env_int("RISK_DAILY_BAR_MAX_AGE_S", 3 * 86400),
        "allow_hedge": _env_int("RISK_ALLOW_HEDGE", 0) == 1,
        "allow_unknown_age": _env_int("RISK_ALLOW_UNKNOWN_AGE", 0) == 1,
        "min_tp_r": float(config.MIN_TP),
        "max_tp_r": float(config.MAX_TP),
        "kill_file": os.environ.get("RISK_KILL_FILE", "data/KILL"),
    }


def kill_active(state=None, path=None):
    """Manual kill switch: a KILL file on disk or a state flag stops new opens."""
    p = path or limits()["kill_file"]
    if state is not None and state.get("kill_switch"):
        return "kill switch set in state"
    try:
        if os.path.exists(p):
            return f"kill file present ({p})"
    except OSError:
        pass
    return None


def triage(state, now=None, lim=None):
    """Session-level stop conditions: pause window and daily loss budget."""
    now = time.time() if now is None else now
    lim = lim or limits()
    if now < state.get("pause_until", 0):
        return "loss-streak pause active"
    if state.get("daily_r", 0.0) <= lim["daily_loss_r"]:
        return "daily loss limit reached"
    return None


# ------------------------------------------------------------ open validation
def validate_open(state, p, now=None, lim=None):
    """Decide whether `p` may open. Returns (ok, reasons).

    `p` is the pending setup dict the engine would otherwise open directly.
    """
    now = time.time() if now is None else now
    lim = lim or limits()
    reasons = []
    manual = p.get("source") == "manual"

    # Pass the effective limits through so an explicit override (including a
    # per-deployment kill-file path) is honoured instead of silently ignored.
    kill = kill_active(state, lim.get("kill_file"))
    if kill:
        reasons.append(kill)

    stop = triage(state, now, lim)
    if stop:
        reasons.append(stop)

    fl = state.get("focus_list", []) or []
    if len(fl) >= lim["max_concurrent"]:
        reasons.append(f"max concurrent positions reached ({len(fl)}/{lim['max_concurrent']})")

    sym = p.get("symbol")
    d = p.get("direction")
    for t in fl:
        if t.get("symbol") == sym and t.get("direction") == d:
            reasons.append(f"duplicate position: {sym} already tracked in that direction")
        elif t.get("symbol") == sym and not lim["allow_hedge"]:
            reasons.append(f"conflicting position: {sym} already open the other way (hedging off)")

    opens = int(state.get("day_opens", 0) or 0)
    if opens >= lim["max_opens_day"]:
        reasons.append(f"daily open cap reached ({opens}/{lim['max_opens_day']})")

    # --- stale card -------------------------------------------------------
    created = p.get("created_at")
    if manual:
        pass  # human-logged position: no card age to check
    elif created is None:
        if not lim["allow_unknown_age"]:
            reasons.append("signal age unknown — refusing to open (fail closed)")
    else:
        age = now - float(created)
        if age > lim["pending_max_age_s"]:
            reasons.append(f"stale signal: card is {int(age)}s old (max {lim['pending_max_age_s']}s)")

    # --- market data age --------------------------------------------------
    bar_ts = p.get("bar_ts")
    if manual:
        pass  # price typed by the owner from the live platform
    elif bar_ts is None:
        if not lim["allow_unknown_age"]:
            reasons.append("market data age unknown — refusing to open (fail closed)")
    else:
        bar_age = now - float(bar_ts)
        cap = lim["daily_bar_max_age_s"] if p.get("tf") == "1d" else lim["bar_max_age_s"]
        if bar_age > cap:
            reasons.append(f"stale market data: last bar {int(bar_age)}s old (max {int(cap)}s)")

    # --- stop-loss validity ----------------------------------------------
    entry, sl = p.get("entry"), p.get("sl")
    if not isinstance(entry, (int, float)) or entry <= 0:
        reasons.append("invalid entry price")
    if not isinstance(sl, (int, float)) or sl <= 0:
        reasons.append("stop loss missing")
    elif isinstance(entry, (int, float)) and entry > 0:
        risk = abs(entry - sl)
        if risk <= 0:
            reasons.append("zero risk: entry equals stop")
        elif d == 1 and sl >= entry:
            reasons.append("stop must be below entry for a BUY")
        elif d == -1 and sl <= entry:
            reasons.append("stop must be above entry for a SELL")

    # --- reward bounds ----------------------------------------------------
    tp_r = p.get("tp")
    if not isinstance(tp_r, (int, float)):
        reasons.append("target missing")
    elif not manual and (tp_r < lim["min_tp_r"] or tp_r > lim["max_tp_r"]):
        reasons.append(
            f"target {tp_r:.2f}R outside configured envelope "
            f"{lim['min_tp_r']:.2f}-{lim['max_tp_r']:.2f}R"
        )

    return (len(reasons) == 0, reasons)


# ------------------------------------------------------------- feed validation
INTERVAL_S = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "60m": 3600, "1h": 3600, "1d": 86400}


def _interval_seconds(interval):
    return INTERVAL_S.get(str(interval), 900)


def validate_bars(bars, interval, now=None, lim=None):
    """Sanity-check a candle series.

    Returns (clean_bars, fatal_problems). `fatal_problems` non-empty means the
    WHOLE feed must be refused (fail closed): the data cannot be trusted for a
    decision. Individually malformed bars are dropped and reported in
    `dropped` (returned as a third value) without failing the feed.

    Guards implemented: malformed rows, non-positive prices, high<low,
    duplicate timestamps, non-monotonic timestamps (repaint/look-ahead),
    timestamps in the future, and staleness.
    """
    now = time.time() if now is None else now
    lim = lim or limits()
    fatal = []
    dropped = []
    clean = []
    seen = set()
    prev_ts = None

    for b in bars or []:
        try:
            ts, o, h, l, c = b[0], b[1], b[2], b[3], b[4]
            ts = float(ts)
            o, h, l, c = float(o), float(h), float(l), float(c)
        except (TypeError, ValueError, IndexError):
            dropped.append("malformed row")
            continue
        if not all(v > 0 for v in (o, h, l, c)):
            dropped.append("non-positive price")
            continue
        if h < l or h < o or h < c or l > o or l > c:
            dropped.append("OHLC inconsistent")
            continue
        if ts in seen:
            dropped.append("duplicate timestamp")
            continue
        if prev_ts is not None and ts <= prev_ts:
            fatal.append("non-monotonic timestamps (repaint / look-ahead risk)")
            break
        if ts > now + 120:
            fatal.append("bar timestamp is in the future")
            break
        seen.add(ts)
        prev_ts = ts
        clean.append((ts, o, h, l, c))

    if not fatal:
        if len(clean) < 2:
            fatal.append("fewer than 2 usable bars")
        else:
            cap = (lim["daily_bar_max_age_s"] if str(interval) == "1d"
                   else max(_interval_seconds(interval) * 3, 600))
            age = now - clean[-1][0]
            if age > cap:
                fatal.append(f"stale feed: last bar {int(age)}s old (max {int(cap)}s)")

    return clean, fatal, dropped


def is_stale(bars, interval, now=None, lim=None):
    """True when the series must not be used for a decision right now."""
    _, fatal, _ = validate_bars(bars, interval, now=now, lim=lim)
    return bool(fatal)
