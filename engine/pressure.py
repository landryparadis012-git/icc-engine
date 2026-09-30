"""
ICC Pressure Engine — V3 port (H4 slot pressure zones) + UNIFIED CARDS
Runs after the main engine scan. Detects slot-wick pressure zones on all
symbols, fires ONE combined card per signal: old-engine pending block first,
pressure setup second, with the SAME take/skip buttons and management flow.
Every blocked WATCH zone is simulated to its outcome — permanent evidence.
"""
import os, json, time, math, sqlite3
from datetime import datetime, timezone
import config
import engine   # reuses fetch_chart, telegram, state helpers

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "memory.db")
PV = "v3-h4p-1"          # pressure config version — isolates this memory

# ---- V3 knobs (match your TradingView settings) ----
MIN_STRENGTH = 6.5
APLUS_LANE   = True
APLUS_MIN    = 6.5
SWEEP_MIN    = 6.5
USE_BIAS     = True
GRACE_HRS    = 4
EXPIRY_SLOTS = 3
CHASE_ATR    = 0.7
MIN_RR       = 1.2
PARTIAL_R    = 1.0
WICK_PCT     = 15.0
WICK_10      = 45.0
TP_WEAK, TP_STRONG = 0.8, 1.4
SL_BUFFER, MIN_STOP = 0.15, 0.3
SESSION_WINDOWS = {"BTC": None}          # default (7,20) UTC; BTC trades all day
BAR_MAX_AGE  = 2 * 3600

# ---------------- slots ----------------
def slot_of(ts):
    h = datetime.fromtimestamp(ts, timezone.utc).hour
    if h < 3:  return 0
    if h < 4:  return -1
    return min(5, 1 + (h - 4) // 4)

def build_slots(h1):
    slots = []
    cur = None
    for t, o, h, l, c in h1:
        s = slot_of(t)
        if s < 0:
            continue
        day = datetime.fromtimestamp(t, timezone.utc).date()
        key = (day, s)
        if cur is None or cur["key"] != key:
            if cur: slots.append(cur)
            cur = {"key": key, "ts": t, "o": o, "h": h, "l": l, "c": c}
        else:
            cur["h"] = max(cur["h"], h); cur["l"] = min(cur["l"], l); cur["c"] = c
    if cur: slots.append(cur)
    return slots

# ---------------- indicators (H1 confirmation filters) ----------------
def ema_series(vals, n):
    k = 2 / (n + 1); out = []; e = vals[0]
    for v in vals:
        e = v * k + e * (1 - k); out.append(e)
    return out

def two_pole(closes, ln=25, flt=20):
    out = []
    for i in range(len(closes)):
        w = closes[max(0, i - ln + 1):i + 1]
        bas = sum(w) / len(w)
        sd = (sum((x - bas) ** 2 for x in w) / len(w)) ** 0.5 if len(w) > 1 else 0
        raw = (closes[i] - bas) / sd if sd > 0 else 0.0
        out.append(raw)
    e1 = ema_series(out, flt)
    return ema_series(e1, flt)

def zlema_series(closes, n=34):
    lag = (n - 1) // 2
    adj = [closes[i] + (closes[i] - closes[i - lag]) if i >= lag else closes[i] for i in range(len(closes))]
    return ema_series(adj, n)

def vidya_trend(bars, cmo_len=20, ln=10, atr_len=200, mult=1.8):
    closes = [b[4] for b in bars]
    vols = [(b[2] + b[3] + 2 * b[4]) / 4 for b in bars]  # proxy vol (Yahoo H1 volume unreliable)
    vid = closes[0]; trend = 0; out = []
    trs = [max(bars[i][2] - bars[i][3], abs(bars[i][2] - bars[i - 1][4]), abs(bars[i][3] - bars[i - 1][4]))
           for i in range(1, len(bars))]
    for i in range(1, len(bars)):
        w = range(max(1, i - cmo_len + 1), i + 1)
        up = sum(max(closes[j] - closes[j - 1], 0) * vols[j] for j in w)
        dn = sum(max(closes[j - 1] - closes[j], 0) * vols[j] for j in w)
        den = up + dn
        kk = abs((up - dn) / den) if den > 0 else 0.0
        vid += (2 / (ln + 1)) * kk * (closes[i] - vid)
        atrv = sum(trs[max(0, i - 1 - atr_len + 1):i]) / max(1, min(atr_len, i))
        if closes[i] > vid + atrv * mult: trend = 1
        elif closes[i] < vid - atrv * mult: trend = -1
        out.append(trend)
    return out, vid

def h1_votes(h1):
    closes = [b[4] for b in h1]
    tp = two_pole(closes); zl = zlema_series(closes); vt, _ = vidya_trend(h1)
    i = len(closes) - 1
    tpo_bull = tp[i] > tp[max(0, i - 2)]; tpo_bear = tp[i] < tp[max(0, i - 2)]
    vid = vt[-1] if vt else 0
    zl_bull = closes[i] > zl[i]; zl_bear = closes[i] < zl[i]
    bull = (1 if tpo_bull else 0) + (1 if vid == 1 else 0) + (1 if zl_bull else 0)
    bear = (1 if tpo_bear else 0) + (1 if vid == -1 else 0) + (1 if zl_bear else 0)
    return bull, bear

# ---------------- scoring (ported from V3) ----------------
def wick_str(frac):
    lo, hi = WICK_PCT / 100, WICK_10 / 100
    if frac < lo: return 0.0
    return min(max((frac - lo) / max(hi - lo, 0.01), 0), 1) * 10

def sig_strength(ws, rej, sweep, struct, fvg_ok, fvg_str, volr):
    bonus = 0.5 if (struct and rej >= 6.0) else 0.0
    return min(10.0, ws * .20 + rej * .15 + (10 if sweep else 0) * .20 +
               (10 if struct else 0) * .15 + (fvg_str if fvg_ok else 0) * .15 +
               (volr if volr is not None else 5.0) * .10 + bonus)

def percentile(pool, x):
    if x is None or x <= 0 or len(pool) < 5: return None
    return sum(1 for v in pool if v <= x) * 10.0 / len(pool)

# ---------------- db ----------------
def pdb(con):
    con.execute("""CREATE TABLE IF NOT EXISTS pmemory(
        id INTEGER PRIMARY KEY AUTOINCREMENT, pv TEXT, symbol TEXT, dirn INTEGER,
        tier TEXT, block_reason TEXT, strength REAL, entry REAL, sl REAL, tp REAL,
        outcome TEXT, r_result REAL, mfe REAL, opened_ts INTEGER, closed_ts INTEGER)""")

def load_pstate(con):
    r = con.execute("SELECT v FROM kv WHERE k='pstate'").fetchone()
    return json.loads(r[0]) if r else {}

def save_pstate(con, st):
    con.execute("INSERT INTO kv(k,v) VALUES('pstate',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (json.dumps(st),))

# ---------------- unified card ----------------
def pending_block(p):
    """render the OLD engine's pending setup (if any) as the first block"""
    if not p:
        return None
    name = config.SYMBOLS[p["symbol"]]["name"]
    d = "BUY" if p["direction"] == 1 else "SELL"
    tier = p.get("tier", "TAKE")
    risk = abs(p["entry"] - p["sl"])
    tp_px = p["entry"] + (1 if p["direction"] == 1 else -1) * risk * p.get("tp", 1.5)
    return (f"<b>1️⃣ ICC ENGINE — {tier} · {name} {d}</b>\n"
            f"Entry {p['entry']:.2f} | SL {p['sl']:.2f} | TP {tp_px:.2f}\n"
            f"Q {p.get('quality', 0):.0f} | MOM {p.get('momentum', 0):.0f}")

def pressure_block(lane, name, dtx, zone, f):
    rr = abs(f["tp"] - f["entry"]) / f["risk"]
    icon = "🌟" if lane == "A+" else "⚡" if lane == "MOMENTUM" else "🟢"
    return (f"<b>2️⃣ {icon} PRESSURE {lane} — {name} {dtx}</b>\n"
            f"Score {zone['strength']:.1f}/10 | {'sweep' if zone['sweep'] else 'zone'}\n"
            f"Entry {f['entry']:.2f} | SL {f['sl']:.2f} | TP {f['tp']:.2f}\n"
            f"R:R {rr:.1f}")

def send_unified(con, lane, sym, d, zone, f):
    """one card: old engine pending first, pressure setup second, same buttons"""
    name = config.SYMBOLS[sym]["name"]
    dtx = "BUY" if d == 1 else "SELL"
    state = engine.load_state(con)
    blocks = []
    pb = pending_block(state.get("pending"))
    if pb:
        blocks.append(pb)
    else:
        blocks.append("<i>1️⃣ ICC ENGINE — no setup this scan</i>")
    blocks.append(pressure_block(lane, name, dtx, zone, f))
    txt = "═══ SETUP CARD ═══\n" + "\n\n".join(blocks) + \
          "\n\n⚠️ Taking the pressure setup REPLACES the old-engine pending (last card wins)."
    # promote the pressure setup into the old engine's pending slot → same buttons, same management
    state["pending"] = {"symbol": sym, "tf": "H4-slot", "direction": d,
                        "tier": f"PRESSURE-{lane}", "entry": f["entry"], "sl": f["sl"],
                        "tp": abs(f["tp"] - f["entry"]) / f["risk"],
                        "feats": {"disp": zone["strength"] / 2, "cont": 1 if zone["sweep"] else 0,
                                  "depth": min(5.0, zone["strength"]), "sweep": 1 if zone["sweep"] else 0,
                                  "vol": zone.get("volr", 50) or 50},
                        "quality": zone["strength"] * 10, "momentum": 60, "conf": 3,
                        "regime": 0, "session": engine.session_tag(int(time.time())), "behav": {}}
    engine.save_state(con, state)
    engine.tg_send(txt, [[{"text": "✅ TAKEN (TAKE)", "callback_data": "T|take"},
                          {"text": "⚠️ TAKEN (RISKY)", "callback_data": "T|risky"}],
                         [{"text": "✖ NOT TAKEN", "callback_data": "T|no"}]])

# ---------------- symbol scan ----------------
def scan_symbol(con, sym, st):
    cfg = config.SYMBOLS[sym]
    h1 = engine.fetch_chart(cfg["yahoo"], "60m", "2mo")
    if len(h1) < 120: return
    if time.time() - h1[-1][0] > BAR_MAX_AGE: return
    h1 = h1[:-1]
    slots = build_slots(h1)
    if len(slots) < 22: return
    ss = st.setdefault(sym, {"zones": [], "virtual": [], "vol_open": [], "vol_h4": [],
                             "atr_hist": [], "last_bar": 0, "last_slot_key": None})
    for s in slots[:-1]:
        rng = s["h"] - s["l"]
        if s["key"][1] == 0:
            pool = ss["vol_open"]
        else:
            pool = ss["vol_h4"]
            ss["atr_hist"].append(rng)
            del ss["atr_hist"][:-14]
        tag = s["key"][0].isoformat() + str(s["key"][1])
        if rng > 0 and (not pool or pool[-1][0] != tag):
            pool.append((tag, rng))
            del pool[:-90]
    slot_atr = sum(ss["atr_hist"]) / len(ss["atr_hist"]) if ss["atr_hist"] else None
    if not slot_atr: return

    prev = slots[-2]; cur = slots[-1]
    skey = str(prev["key"])
    if ss["last_slot_key"] != skey:
        ss["last_slot_key"] = skey
        rng = prev["h"] - prev["l"]
        if rng > 0:
            body_top, body_bot = max(prev["o"], prev["c"]), min(prev["o"], prev["c"])
            up_pct = min(max(prev["h"] - body_top, 0) / rng, 1)
            lo_pct = min(max(body_bot - prev["l"], 0) / rng, 1)
            i2 = [i for i, s in enumerate(slots) if s["key"] == prev["key"]][0]
            s1 = slots[i2 - 1] if i2 >= 1 else None
            s2 = slots[i2 - 2] if i2 >= 2 else None
            bull_sweep = bool(s1 and prev["l"] < s1["l"] and prev["c"] > s1["l"])
            bear_sweep = bool(s1 and prev["h"] > s1["h"] and prev["c"] < s1["h"])
            bull_struct = bool(s2 and prev["c"] > s2["h"])
            bear_struct = bool(s2 and prev["c"] < s2["l"])
            fvg_ok = fvg_str = 0.0; fvg_dir = 0
            if s2 and slot_atr > 0:
                if prev["l"] > s2["h"] and prev["c"] > prev["o"] and abs(prev["c"] - prev["o"]) / rng >= .55:
                    fvg_dir, fvg_ok = 1, True
                    r = (prev["l"] - s2["h"]) / slot_atr
                    fvg_str = 8 if r >= 1.5 else 6 if r >= 1 else 4.5 if r >= .75 else 3 if r >= .5 else 1.5
                elif prev["h"] < s2["l"] and prev["c"] < prev["o"] and abs(prev["c"] - prev["o"]) / rng >= .55:
                    fvg_dir, fvg_ok = -1, True
                    r = (s2["l"] - prev["h"]) / slot_atr
                    fvg_str = 8 if r >= 1.5 else 6 if r >= 1 else 4.5 if r >= .75 else 3 if r >= .5 else 1.5
            volr = percentile([p[1] for p in (ss["vol_open"] if prev["key"][1] == 0 else ss["vol_h4"])], rng)
            h4closes = [s["c"] for s in slots[:-1]]
            e50 = ema_series(h4closes, 50)[-1]
            price = h1[-1][4]
            bias_up = price > e50; bias_dn = price < e50
            now = int(time.time())
            expiry = now + EXPIRY_SLOTS * 4 * 3600
            zs = ss["zones"]
            if up_pct >= WICK_PCT / 100 and body_top > prev["l"]:
                ws = wick_str(up_pct)
                rej = min(10, (((up_pct - WICK_PCT/100) / max(WICK_10/100 - WICK_PCT/100, .01)) * .45 +
                               ((prev["h"] - prev["c"]) / rng) * .40 +
                               (abs(prev["c"] - prev["o"]) / rng) * .15) * 10)
                comp = sig_strength(ws, rej, bear_sweep, bear_struct, fvg_ok and fvg_dir == -1, fvg_str, volr)
                aplus = APLUS_LANE and bear_sweep and bear_struct and (fvg_ok and fvg_dir == -1)
                ok = comp >= MIN_STRENGTH and bear_sweep and bear_struct and (not USE_BIAS or bias_dn)
                ok = ok or (aplus and comp >= APLUS_MIN and (not USE_BIAS or bias_dn))
                if ok and (not zs or zs[-1]["dirn"] != -1 or comp >= zs[-1]["strength"]):
                    zs.append({"dirn": -1, "top": prev["h"], "bot": body_top, "strength": comp,
                               "atr": slot_atr, "expiry": expiry, "touched": False, "touch_ts": None,
                               "sweep": bear_sweep, "aplus": bool(aplus), "sent": False, "volr": volr})
            if lo_pct >= WICK_PCT / 100 and body_bot < prev["h"]:
                ws = wick_str(lo_pct)
                rej = min(10, (((lo_pct - WICK_PCT/100) / max(WICK_10/100 - WICK_PCT/100, .01)) * .45 +
                               ((prev["c"] - prev["l"]) / rng) * .40 +
                               (abs(prev["c"] - prev["o"]) / rng) * .15) * 10)
                comp = sig_strength(ws, rej, bull_sweep, bull_struct, fvg_ok and fvg_dir == 1, fvg_str, volr)
                aplus = APLUS_LANE and bull_sweep and bull_struct and (fvg_ok and fvg_dir == 1)
                ok = comp >= MIN_STRENGTH and bull_sweep and bull_struct and (not USE_BIAS or bias_up)
                ok = ok or (aplus and comp >= APLUS_MIN and (not USE_BIAS or bias_up))
                if ok and (not zs or zs[-1]["dirn"] != 1 or comp >= zs[-1]["strength"]):
                    zs.append({"dirn": 1, "top": body_bot, "bot": prev["l"], "strength": comp,
                               "atr": slot_atr, "expiry": expiry, "touched": False, "touch_ts": None,
                               "sweep": bull_sweep, "aplus": bool(aplus), "sent": False, "volr": volr})
            del zs[:-4]

    bull_v, bear_v = h1_votes(h1)
    s1, s2 = slots[-2], slots[-3] if len(slots) >= 3 else None
    lastbar_ts = ss["last_bar"]
    new_bars = [b for b in h1 if b[0] > lastbar_ts]
    if not new_bars: return
    ss["last_bar"] = h1[-1][0]

    def fire(zone, bar):
        d = zone["dirn"]
        entry = bar[4]
        if d == 1:
            sl = min(zone["bot"] - SL_BUFFER * zone["atr"], entry - MIN_STOP * zone["atr"])
            chase = entry <= zone["top"] + CHASE_ATR * zone["atr"]
        else:
            sl = max(zone["top"] + SL_BUFFER * zone["atr"], entry + MIN_STOP * zone["atr"])
            chase = entry >= zone["bot"] - CHASE_ATR * zone["atr"]
        if not chase: return None
        risk = abs(entry - sl)
        if risk <= 0: return None
        tp = None
        for s in (s1, s2):
            if d == 1 and s and s["h"] > entry:
                tp = s["h"] if tp is None else min(tp, s["h"])
            if d == -1 and s and s["l"] < entry:
                tp = s["l"] if tp is None else max(tp, s["l"])
        atr_tp = entry + d * (TP_WEAK + (TP_STRONG - TP_WEAK) * zone["strength"] / 10) * zone["atr"]
        if tp is None or abs(tp - entry) / risk < MIN_RR: tp = atr_tp
        if abs(tp - entry) / risk < MIN_RR: return None
        return {"entry": entry, "sl": sl, "tp": tp, "risk": risk}

    win = SESSION_WINDOWS.get("BTC" if "BTC" in sym else sym, (7, 20))
    hour_now = datetime.fromtimestamp(h1[-1][0], timezone.utc).hour
    session_ok = True if win is None else (win[0] <= hour_now < win[1])
    h4closes = [s["c"] for s in slots[:-1]]
    e50 = ema_series(h4closes, 50)[-1]
    price = h1[-1][4]

    for zone in ss["zones"][:]:
        d = zone["dirn"]
        if time.time() > zone["expiry"]:
            ss["zones"].remove(zone); continue
        for bar in new_bars:
            touched_now = (bar[3] <= zone["top"] and bar[2] >= zone["bot"]) if d == 1 else \
                          (bar[2] >= zone["bot"] and bar[3] <= zone["top"])
            if touched_now and not zone["touched"]:
                zone["touched"] = True; zone["touch_ts"] = bar[0]
            invalid = bar[4] < zone["bot"] if d == 1 else bar[4] > zone["top"]
            if invalid or (zone["touched"] and time.time() - zone["touch_ts"] > GRACE_HRS * 3600):
                if not zone["sent"] and zone["touched"]:
                    ss["virtual"].append({"sym": sym, "dirn": d, "tier": "WATCH",
                                          "reason": zone.get("reason", "UNTOUCHED-EXPIRY"),
                                          "strength": zone["strength"], "opened_ts": int(time.time()),
                                          "mfe": 0.0})
                ss["zones"].remove(zone); break
        if zone not in ss["zones"]: continue
        bias_ok = price > e50 if d == 1 else price < e50
        votes = bull_v if d == 1 else bear_v
        bar = h1[-1]
        lane = None
        if zone["touched"] and votes >= 1:
            lane = "RETEST"
        elif zone["sweep"] and (zone["aplus"] or zone["strength"] >= SWEEP_MIN) and votes >= 1:
            lane = "A+" if zone["aplus"] else "MOMENTUM"
        if lane and not session_ok:
            if not zone.get("reason"): zone["reason"] = "NO-SESSION"
            continue
        if lane and not bias_ok and USE_BIAS:
            if not zone.get("reason"): zone["reason"] = "COUNTER-TREND"
            continue
        if lane:
            f = fire(zone, bar)
            if f:
                ss["virtual"].append({"sym": sym, "dirn": d, "tier": lane, "reason": None,
                                      "strength": zone["strength"], "entry": f["entry"], "sl": f["sl"],
                                      "tp": f["tp"], "risk": f["risk"], "partial": False,
                                      "opened_ts": int(time.time()), "mfe": 0.0, "be": False})
                if not zone["sent"]:
                    send_unified(con, lane, sym, d, zone, f)
                    zone["sent"] = True
                ss["zones"].remove(zone)
        else:
            if not zone.get("reason"):
                r = "NO-SESSION" if not session_ok else \
                    "COUNTER-TREND" if (not bias_ok and USE_BIAS) else \
                    "LOW-SCORE" if (zone["strength"] < MIN_STRENGTH and not zone["aplus"]) else \
                    "NO-CONFIRM" if votes < 1 else None
                if r: zone["reason"] = r

def resolve_virtual(con, st):
    keep = []
    for v in st.get("virtual", []):
        sym = v["sym"]; cfg = config.SYMBOLS[sym]
        h1 = engine.fetch_chart(cfg["yahoo"], "60m", "5d")[:-1]
        bars = [b for b in h1 if b[0] * 1000 > v["opened_ts"] * 1000 - 3600 * 1000]
        done = None; d = v["dirn"]
        for t, o, h, l, c in bars:
            if "entry" not in v:
                v["entry"] = c; v["risk"] = max(abs(c - v.get("sl_est", c * .995)), 1e-9)
                v["tp"] = c + d * v["risk"] * 1.5; v["sl"] = c - d * v["risk"]
            risk = v["risk"]
            r = (h - v["entry"]) / risk if d == 1 else (v["entry"] - l) / risk
            v["mfe"] = max(v["mfe"], r)
            sl_eff = v["entry"] if v.get("partial") else v["sl"]
            sl_hit = l <= sl_eff if d == 1 else h >= sl_eff
            tp_hit = h >= v["tp"] if d == 1 else l <= v["tp"]
            if not v.get("partial"):
                if sl_hit: done = -1.0
                elif tp_hit: done = abs(v["tp"] - v["entry"]) / risk
                elif r >= PARTIAL_R:
                    v["partial"] = True; done = None
            else:
                if sl_hit: done = 0.0
                elif tp_hit: done = PARTIAL_R * 0.5 + 0.5 * abs(v["tp"] - v["entry"]) / risk
            if done is not None: break
        elapsed = time.time() - v["opened_ts"]
        if done is None and elapsed > 36 * 3600:
            price = h1[-1][4] if h1 else v["entry"]
            done = (price - v["entry"]) / v["risk"] if d == 1 else (v["entry"] - price) / v["risk"]
        if done is None:
            keep.append(v); continue
        outcome = "TP" if done > 0.05 else "BE" if done > -0.05 else "SL"
        con.execute("INSERT INTO pmemory(pv,symbol,dirn,tier,block_reason,strength,entry,sl,tp,outcome,r_result,mfe,opened_ts,closed_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (PV, sym, d, v["tier"], v.get("reason"), v.get("strength"),
                     v.get("entry"), v.get("sl"), v.get("tp"), outcome, round(done, 3),
                     round(v["mfe"], 3), v["opened_ts"], int(time.time())))
        if v["tier"] == "WATCH":
            name = cfg["name"]; dtx = "BUY" if d == 1 else "SELL"
            engine.tg_send(f"👁 WATCH resolved — {name} {dtx} [{v.get('reason','?')}] "
                           f"{outcome} {done:+.2f}R (evidence logged)")
        con.commit()
    st["virtual"] = keep

def main():
    con = sqlite3.connect(DB_PATH)
    pdb(con)
    st = load_pstate(con)
    for sym in config.SYMBOLS:
        try:
            scan_symbol(con, sym, st)
        except Exception as e:
            print(f"[pressure] {sym} error: {e}")
    resolve_virtual(con, st)
    save_pstate(con, st)
    con.commit(); con.close()
    n = 0
    try:
        con = sqlite3.connect(DB_PATH)
        n = con.execute("SELECT COUNT(*) FROM pmemory").fetchone()[0]
        con.close()
    except Exception:
        pass
    print(f"[pressure] scan complete | evidence rows: {n}")

if __name__ == "__main__":
    main()
