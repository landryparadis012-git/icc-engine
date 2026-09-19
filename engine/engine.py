"""
ICC Cloud Engine — GitHub Build 1 (scheduled scanner + Telegram advisories)
Yahoo data -> MTF features -> memory-matched decisions -> alerts with buttons.
Focus mode tracks manually taken trades and sends management advisories.
All execution is manual: the engine scans, decides, tracks, advises.

Included brainstorm features:
  evidence gate | expectancy ranking | config version stamping
  regime+session tagged memory | raw-vs-managed outcomes | replay learning
  focus mode (Telegram buttons) | A+ break-glass gate | correlation slots
  risk firewall (loss-streak pause + daily loss limit) | HTF-confirm management
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
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
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
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not tok:
        return []
    try:
        resp = http_json(config.TG_API.format(tok, "getUpdates") + f"?offset={offset}&timeout=0", headers=UA)
    except Exception as e:
        print("[tg] getUpdates failed:", e)
        return []
    if isinstance(resp, dict):
        return resp.get("result", []) or []
    return []

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
    con.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    return con

def load_state(con):
    row = con.execute("SELECT v FROM kv WHERE k='state'").fetchone()
    return json.loads(row[0]) if row else {}

def save_state(con, state):
    con.execute("INSERT INTO kv(k,v) VALUES('state',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (json.dumps(state),))

def remember(con, cand, decision, outcome_type, r_result, mfe, mae, opened_ts):
    con.execute("""INSERT INTO memory(config_version,symbol,direction,tf,disp,cont,depth,sweep,vol,
        quality,momentum,regime,session,decision,entry,sl,tp,outcome_type,r_result,mfe,mae,opened_ts,closed_ts)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (config.CONFIG_VERSION, cand["symbol"], cand["direction"], cand.get("tf", "15m"),
         cand["feats"]["disp"], cand["feats"]["cont"], cand["feats"]["depth"],
         cand["feats"]["sweep"], cand["feats"]["vol"],
         cand.get("quality", 50), cand.get("momentum", 50), cand.get("regime", 0),
         cand.get("session", "?"), decision, cand["entry"], cand["sl"], cand.get("tp", 1.5),
         outcome_type, r_result, mfe, mae, opened_ts, int(time.time())))

# ---------------- memory matching ----------------
def match_memory(con, cand):
    rows = con.execute("""SELECT disp,cont,depth,sweep,vol,r_result,opened_ts,regime,session
        FROM memory WHERE config_version=? AND symbol=? AND direction=? AND outcome_type IS NOT NULL""",
        (config.CONFIG_VERSION, cand["symbol"], cand["direction"])).fetchall()
    f = cand["feats"]
    w_cnt = w_win = sum_r = best = 0.0
    for d, c, dep, sw, v, res, ts, reg, sess in rows:
        if config.REQUIRE_REGIME_MATCH and reg != cand.get("regime", 0):
            continue
        if config.REQUIRE_SESSION_MATCH and sess != cand.get("session", "?"):
            continue
        s1 = max(0.0, 100 - abs(f["disp"] - d) * 50)
        s2 = max(0.0, 100 - abs(f["cont"] - c))
        s3 = max(0.0, 100 - abs(f["depth"] - dep) * 20)
        s4 = max(0.0, 100 - abs(f["sweep"] - sw))
        s5 = max(0.0, 100 - abs(f["vol"] - v))
        sim = s1 * 0.25 + s2 * 0.25 + s3 * 0.20 + s4 * 0.15 + s5 * 0.15
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
    exp = sum_r / w_cnt if w_cnt > 0 else 0.0
    tp = min(config.MAX_TP, max(config.MIN_TP, exp)) if w_cnt > 0 else 1.5
    return {"count": w_cnt, "wr": (w_win / w_cnt * 100) if w_cnt > 0 else 0.0,
            "exp": exp, "sim": best, "tp": tp, "raw": len(rows)}

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
          "s15": s15, "s60": s60, "sd": sd}
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

def send_setup_alert(state, cand, mem, decision, reason, edge, tag=""):
    d = "BUY" if cand["direction"] == 1 else "SELL"
    icon = "🟢" if decision == 1 else "🟠"
    tier = "TAKE" if decision == 1 else "RISKY"
    name = config.SYMBOLS[cand["symbol"]]["name"]
    tp_px = cand["entry"] + (1 if cand["direction"] == 1 else -1) * abs(cand["entry"] - cand["sl"]) * cand["tp"]
    txt = (f"{icon} <b>{tier} — {name} {d}</b> {tag}\n"
           f"Entry {cand['entry']:.2f} | SL {cand['sl']:.2f} | TP {tp_px:.2f} ({cand['tp']:.2f}R)\n"
           f"Memory {mem['count']:.0f} | WR {mem['wr']:.0f}% | EXP {mem['exp']:+.2f}R | SIM {mem['sim']:.0f}%\n"
           f"Q {cand['quality']:.0f} | MOM {cand['momentum']:.0f} | MTF {cand['conf']}/4 | {cand['session']} reg={cand['regime']}\n"
           f"Reason: {reason} | Edge {edge:.0f} | v{config.CONFIG_VERSION}")
    tg_send(txt, [[{"text": "✅ TAKEN (TAKE)", "callback_data": "T|take"},
                   {"text": "⚠️ TAKEN (RISKY)", "callback_data": "T|risky"}],
                  [{"text": "✖ NOT TAKEN", "callback_data": "T|no"}]])

def corr_blocked(sym_a, sym_b):
    return any(sym_a in cl and sym_b in cl for cl in config.CLUSTERS)

def a_plus_check(cand, mem, state):
    if mem["count"] < 30 or mem["exp"] < 0.25 or cand["quality"] < 80 or cand["conf"] < 4:
        return False
    f = state.get("focus")
    if f and f["symbol"] == cand["symbol"] and f["direction"] == cand["direction"]:
        return "BLOCKED-DUP (same opportunity open)"
    if f and corr_blocked(f["symbol"], cand["symbol"]):
        return "BLOCKED-CORR (correlated exposure open)"
    return True

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
    focus = state.get("focus")
    ranked = sorted(cands, key=lambda cm: (cm[1]["exp"] if cm[1]["count"] > 0 else -9.0, cm[0]["quality"]), reverse=True)
    cand, mem = ranked[0]  # best-or-none
    key = f"{cand['symbol']}|{cand['direction']}"
    if time.time() < state.get("cooldowns", {}).get(key, 0):
        return
    if focus and focus["symbol"] == cand["symbol"] and focus["direction"] == cand["direction"]:
        return
    decision, reason, edge = decide(cand, mem)
    tag = ""
    if focus:
        ap = a_plus_check(cand, mem, state)
        if ap is True:
            tag = "🟢 A+ BREAK-GLASS"
        elif isinstance(ap, str):
            tg_send(f"🟡 A+ candidate on {config.SYMBOLS[cand['symbol']]['name']} suppressed: {ap}")
            return
    if decision == 3:
        rp = dict(cand)
        rp["decision"] = 3
        rp["risk"] = abs(cand["entry"] - cand["sl"])
        rp["tp_r"] = cand["tp"]
        rp["opened_ts"] = int(time.time())
        state.setdefault("replays", []).append(rp)
        state["replays"] = state["replays"][-50:]
        print(f"[setup] {cand['symbol']} SKIP ({reason}) — queued for raw replay")
        return
    send_setup_alert(state, cand, mem, decision, reason, edge, tag)
    cd = state.get("cooldowns", {})
    cd[key] = time.time() + config.COOLDOWN_H * 3600
    state["cooldowns"] = cd
    state["pending"] = {k: cand[k] for k in ("symbol", "tf", "direction", "entry", "sl", "tp",
                                             "feats", "quality", "momentum", "conf", "regime", "session")}
    state["pending"]["tier"] = "TAKE" if decision == 1 else "RISKY"
    print(f"[setup] {cand['symbol']} {decision} alerted ({reason})")

def manage_focus(state, analyses):
    f = state.get("focus")
    if not f:
        return
    an = analyses.get(f["symbol"])
    if not an:
        return
    price, dirn = an["price"], f["direction"]
    r = (price - f["entry"]) / f["risk"] if dirn == 1 else (f["entry"] - price) / f["risk"]
    f["mfe"] = max(f.get("mfe", 0.0), r)
    f["mae"] = min(f.get("mae", 0.0), r)
    f["last_r"] = r
    s15, s60 = an["s15"], an["s60"]
    htf_ok = s60["bull"] if dirn == 1 else s60["bear"]
    rng = max(an["h"] - an["l"], 1e-9)
    dl = (price - an["l"]) / rng * 100 if dirn == 1 else (an["h"] - price) / rng * 100
    disp_now = min(5.0, abs(price - an["o"]) / max(s15["atr"], 1e-9))
    mom = (30 if (s15["bull"] if dirn == 1 else s15["bear"]) else 0) + (30 if htf_ok else 0) \
        + dl / 100 * 20 + min(20.0, disp_now / 1.5 * 20)
    f["momentum"] = mom
    msgs = []
    if not f.get("be_done") and r >= config.MIN_PROTECT_R and (mom < config.WEAK or f["mfe"] >= 1.0):
        be = f["entry"] + dirn * f["risk"] * config.BE_OFFSET_R
        msgs.append(f"🛡 MOVE SL → BREAK-EVEN @ {be:.2f}")
        f["be_done"] = True
    if not f.get("reduced") and r > 0.2 and mom < config.WEAK and not htf_ok:
        newtp = max(config.MIN_TP, min(config.MAX_TP, f["mfe"] * 0.8))
        f["tp_r"] = min(f["tp_r"], newtp)
        msgs.append(f"✂ REDUCE TP → {tp_price_from(f):.2f} ({f['tp_r']:.2f}R) — momentum weak, 1h NOT confirming")
        f["reduced"] = True
    if not f.get("extended") and r >= 1.0 and htf_ok and mom >= 70:
        newtp = min(config.MAX_TP, max(f["tp_r"], f["mfe"] if f["mfe"] > 1.5 else f["tp_r"] * 1.2))
        f["tp_r"] = newtp
        f["extended"] = True
        msgs.append(f"🚀 EXTEND TP → {tp_price_from(f):.2f} ({newtp:.2f}R) — 1h confirms the move")
    if f.get("be_done") and r > 0.15 and mom < config.SEVERE:
        msgs.append("🚨 EXIT ZONE — momentum gone after protection. Consider manual exit.")
    sl_hit = price <= f["sl"] if dirn == 1 else price >= f["sl"]
    tp_hit = price >= tp_price_from(f) if dirn == 1 else price <= tp_price_from(f)
    if sl_hit:
        msgs.append("⚠️ Price at/beyond your SL level — confirm with /close <R>")
    if tp_hit:
        msgs.append("🎯 Price at/near TP — /close to log it")
    if not msgs:
        f["pings"] = f.get("pings", 0) + 1
        if f["pings"] >= 8:
            f["pings"] = 0
            msgs.append(f"POSITION OPEN — SYNCING | R {r:+.2f} | MFE {f['mfe']:+.2f}")
    if msgs:
        name = config.SYMBOLS[f["symbol"]]["name"]
        head = f"⚙️ MANAGING {name} {'BUY' if dirn == 1 else 'SELL'} ({f['tier']}) | R {r:+.2f}\n"
        tg_send(head + "\n".join(msgs),
                [[{"text": "🏁 CLOSED — log it", "callback_data": "C|close"}]])

def close_focus(con, state, r_override=None):
    f = state.get("focus")
    if not f:
        return "no open trade"
    r = r_override if r_override is not None else f.get("last_r", 0.0)
    decision = 1 if f["tier"] == "TAKE" else 2
    cand = {"symbol": f["symbol"], "tf": f.get("tf", "15m"), "direction": f["direction"],
            "entry": f["entry"], "sl": f["sl"], "tp": f["tp_r"], "feats": f["feats"],
            "quality": f.get("quality", 50), "momentum": f.get("momentum", 50),
            "conf": f.get("conf", 2), "regime": f.get("regime", 0), "session": f.get("session", "?")}
    remember(con, cand, decision, "managed", r, f.get("mfe", 0.0), f.get("mae", 0.0), f["opened_ts"])
    state["loss_streak"] = (state.get("loss_streak", 0) + 1) if r < 0 else 0
    if state["loss_streak"] >= config.MAX_LOSS_STREAK:
        state["pause_until"] = time.time() + config.PAUSE_SCANS * config.CADENCE_MIN * 60
        tg_send(f"🛑 {config.MAX_LOSS_STREAK} losses in a row — engine paused for {config.PAUSE_SCANS} scans.")
    state["daily_r"] = state.get("daily_r", 0.0) + r
    state["focus"] = None
    return f"logged {r:+.2f}R (managed, {f['tier']})"

# ---------------- telegram updates ----------------
def handle_ack(con, state, data):
    if data in ("T|take", "T|risky"):
        p = state.get("pending")
        if not p:
            tg_send("No pending setup to acknowledge.")
            return
        state["focus"] = {"symbol": p["symbol"], "tf": p["tf"], "direction": p["direction"],
                          "tier": p["tier"], "entry": p["entry"], "sl": p["sl"],
                          "risk": abs(p["entry"] - p["sl"]), "tp_r": p["tp"], "feats": p["feats"],
                          "quality": p["quality"], "momentum": p["momentum"], "conf": p["conf"],
                          "regime": p["regime"], "session": p["session"],
                          "opened_ts": int(time.time()), "mfe": 0.0, "mae": 0.0,
                          "be_done": False, "reduced": False, "extended": False, "pings": 0}
        f = state["focus"]
        name = config.SYMBOLS[f["symbol"]]["name"]
        d = "BUY" if f["direction"] == 1 else "SELL"
        tg_send(f"🔒 FOCUS MODE — tracking {name} {d} ({f['tier']})\n"
                f"Entry {f['entry']:.2f} | SL {f['sl']:.2f} | TP {tp_price_from(f):.2f}\n"
                f"Management advisories incoming. Routine alerts now muted except A+ break-glass.",
                [[{"text": "🏁 CLOSED — log it", "callback_data": "C|close"}]])
    elif data == "T|no":
        state["pending"] = None
        tg_send("Noted — not taken. Similar setups stay quiet for the cooldown period.")
    elif data == "C|close":
        msg = close_focus(con, state, None)
        tg_send(f"🏁 {msg} | Focus mode off — full scanning resumed.")

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
            state["focus"] = {"symbol": sym, "tf": "manual", "direction": d, "tier": tier,
                              "entry": entry, "sl": sl, "risk": abs(entry - sl),
                              "tp_r": abs(tp - entry) / max(abs(entry - sl), 1e-9),
                              "feats": {"disp": 0, "cont": 0, "depth": 0, "sweep": 0, "vol": 50},
                              "quality": 50, "momentum": 50, "conf": 2, "regime": 0,
                              "session": session_tag(time.time()), "opened_ts": int(time.time()),
                              "mfe": 0.0, "mae": 0.0, "be_done": False, "reduced": False,
                              "extended": False, "pings": 0}
            tg_send(f"🔒 FOCUS MODE — tracking manual {sym} {'BUY' if d == 1 else 'SELL'} ({tier}).",
                    [[{"text": "🏁 CLOSED — log it", "callback_data": "C|close"}]])
        else:
            tg_send("Format: /taken XAUUSD BUY 2650.5 2645.0 2660.0 RISKY")
    elif txt.startswith("/close"):
        parts = txt.split()
        r_override = None
        if len(parts) > 1:
            try:
                r_override = float(parts[1])
            except ValueError:
                pass
        tg_send(f"🏁 {close_focus(con, state, r_override)} | Focus off.")
    elif txt == "/status":
        f = state.get("focus")
        tg_send(f"ICC {config.CONFIG_VERSION} | focus: {f['symbol'] if f else 'none'} | "
                f"daily R {state.get('daily_r', 0):+.2f} | streak {state.get('loss_streak', 0)} | "
                f"memory rows: managed+raw")

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

# ---------------- replay learning ----------------
def resolve_replays(con, state, analyses):
    keep = []
    for rp in state.get("replays", []):
        an = analyses.get(rp["symbol"])
        if not an:
            keep.append(rp)
            continue
        price, dirn = an["price"], rp["direction"]
        r = (price - rp["entry"]) / rp["risk"] if dirn == 1 else (rp["entry"] - price) / rp["risk"]
        tp_px = rp["entry"] + (1 if dirn == 1 else -1) * rp["risk"] * rp["tp_r"]
        done = None
        if (price <= rp["sl"]) if dirn == 1 else (price >= rp["sl"]):
            done = -1.0
        elif (price >= tp_px) if dirn == 1 else (price <= tp_px):
            done = rp["tp_r"]
        elif time.time() - rp["opened_ts"] > config.REPLAY_TTL_H * 3600:
            done = round(r, 3)
        if done is None:
            keep.append(rp)
            continue
        remember(con, rp, rp.get("decision", 3), "raw", done, max(r, 0.0), min(r, 0.0), rp["opened_ts"])
        print(f"[replay] {rp['symbol']} resolved {done:+.2f}R (raw)")
    state["replays"] = keep

# ---------------- main ----------------
def main():
    con = db_connect()
    state = load_state(con)
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
