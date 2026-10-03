"""hub_emit — write trade lifecycle events into the engine outbox.
Used by engine.py close_focus. Never raises into engine logic."""

import json
import time


def focus_trade_id(f):
    return f"{f.get('symbol', '?')}|{f.get('direction', 0)}|{f.get('opened_ts', 0)}"


def _insert(con, ev_type, payload):
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS outbox(
                event_id TEXT PRIMARY KEY, type TEXT, payload TEXT,
                created_at TEXT, sent INTEGER DEFAULT 0)"""
        )
        p = dict(payload)
        p["schema_version"] = 1
        p["source"] = "engine"
        p["event_id"] = f"eng-{p['trade_id']}-{ev_type}"
        p.setdefault("occurred_at", int(time.time()))
        con.execute(
            "INSERT OR REPLACE INTO outbox(event_id,type,payload,created_at,sent) VALUES(?,?,?,?,0)",
            (p["event_id"], ev_type, json.dumps(p),
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        con.commit()
        print(f"[hub] queued {ev_type} for {p['trade_id']}")
    except Exception as e:
        print(f"[hub] emit {ev_type} failed: {e}")


def emit_opened(con, f):
    p = {
        "trade_id": focus_trade_id(f),
        "symbol": f.get("symbol", "?"),
        "side": "BUY" if f.get("direction") == 1 else "SELL",
        "tier": str(f.get("tier", "TAKE")).lower(),
        "strategy": "core",
        "entry": f.get("entry"),
        "sl": f.get("sl"),
        "occurred_at": int(f.get("opened_ts") or time.time()),
    }
    _insert(con, "trade.opened", p)


def emit_closed(con, f, r, path=None):
    path = path or {}
    p = {
        "trade_id": focus_trade_id(f),
        "symbol": f.get("symbol", "?"),
        "side": "BUY" if f.get("direction") == 1 else "SELL",
        "tier": str(f.get("tier", "TAKE")).lower(),
        "strategy": "core",
        "entry": f.get("entry"),
        "realized_r": round(float(r), 3),
        "mfe_r": round(float(f.get("mfe", 0.0)), 3),
        "mae_r": round(float(f.get("mae", 0.0)), 3),
        "peak_r": round(float(path.get("peak_r") or f.get("peak_r", 0.0)), 3),
        "occurred_at": int(time.time()),
    }
    _insert(con, "trade.closed", p)
