"""OpsHub sync: queues + sends engine events to the hub (HMAC-signed).
Sources: (1) outbox rows from hub_emit (exact closes), (2) focus-list diff
(safety net for opens / missed closes). Never crashes the workflow."""

import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
import urllib.request

DB_PATH = os.path.join("data", "memory.db")
HUB_URL = os.environ.get("HUB_URL", "").rstrip("/")
SECRET = os.environ.get("HUB_WEBHOOK_SECRET", "")

try:
    import config as _cfg
    ENGINE_VERSION = str(getattr(_cfg, "CONFIG_VERSION", ""))
except Exception:
    ENGINE_VERSION = ""

try:
    from hub_emit import focus_trade_id
except Exception:
    def focus_trade_id(f):
        return f"{f.get('symbol', '?')}|{f.get('direction', 0)}|{f.get('opened_ts', 0)}"


def ensure_outbox(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS outbox(
            event_id TEXT PRIMARY KEY, type TEXT, payload TEXT,
            created_at TEXT, sent INTEGER DEFAULT 0)"""
    )
    conn.commit()


def pending_events(conn, limit=30):
    cur = conn.execute(
        "SELECT event_id, type, payload FROM outbox WHERE sent = 0 ORDER BY rowid LIMIT ?",
        (limit,),
    )
    return cur.fetchall()


def mark_sent(conn, event_id):
    conn.execute("UPDATE outbox SET sent = 1 WHERE event_id = ?", (event_id,))
    conn.commit()


def prune_old(conn, keep_days=30):
    cutoff = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - keep_days * 86400)
    )
    conn.execute("DELETE FROM outbox WHERE sent = 1 AND created_at < ?", (cutoff,))
    conn.commit()


def event_exists(conn, ev_type, trade_id):
    rows = conn.execute("SELECT payload FROM outbox WHERE type=?", (ev_type,)).fetchall()
    for (p,) in rows:
        try:
            if json.loads(p).get("trade_id") == trade_id:
                return True
        except Exception:
            pass
    return False


def insert_event(conn, ev_type, payload):
    p = dict(payload)
    p["schema_version"] = 1
    p["source"] = "engine"
    p["engine_version"] = ENGINE_VERSION
    p["event_id"] = f"hubsync-{p['trade_id']}-{ev_type}"
    con_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.execute(
        "INSERT OR REPLACE INTO outbox(event_id,type,payload,created_at,sent) VALUES(?,?,?,?,0)",
        (p["event_id"], ev_type, json.dumps(p), con_str),
    )
    conn.commit()
    print(f"[hub] diff queued {ev_type} for {p['trade_id']}")


def diff_focus(conn):
    """Compare current focus_list with the stored snapshot; emit opens/closes."""
    row = conn.execute("SELECT v FROM kv WHERE k='state'").fetchone()
    state = json.loads(row[0]) if row else {}
    focus_list = state.get("focus_list", [])

    snap_row = conn.execute("SELECT v FROM kv WHERE k='hub_focus_snapshot'").fetchone()
    snap = {t["trade_id"]: t for t in (json.loads(snap_row[0]) if snap_row else [])}

    current = {}
    for f in focus_list:
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
        current[p["trade_id"]] = p
        if p["trade_id"] not in snap and not event_exists(conn, "trade.opened", p["trade_id"]):
            insert_event(conn, "trade.opened", p)

    for tid, old in snap.items():
        if tid not in current and not event_exists(conn, "trade.closed", tid):
            p = dict(old)
            p["occurred_at"] = int(time.time())
            insert_event(conn, "trade.closed", p)

    conn.execute(
        "INSERT INTO kv(k,v) VALUES('hub_focus_snapshot',?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (json.dumps(list(current.values())),),
    )
    conn.commit()


def send(payload_json):
    body = payload_json.encode("utf-8")
    mac = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256)
    req = urllib.request.Request(
        HUB_URL + "/ingest",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-hub-signature-256": "sha256=" + mac.hexdigest(),
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def main():
    if not HUB_URL or not SECRET:
        print("hub_sync: HUB_URL / HUB_WEBHOOK_SECRET not set, skipping")
        return
    if not os.path.exists(DB_PATH):
        print("hub_sync: no memory.db yet, nothing to send")
        return

    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_outbox(conn)
        try:
            diff_focus(conn)
        except Exception as e:
            print(f"hub_sync: diff skipped: {e}")

        rows = pending_events(conn)
        if not rows:
            print("hub_sync: outbox empty")
            return

        ok = fail = 0
        for event_id, ev_type, payload in rows:
            try:
                status = send(payload)
                if status == 200:
                    mark_sent(conn, event_id)
                    ok += 1
                    print(f"hub_sync: sent {ev_type} -> 200")
                else:
                    fail += 1
                    print(f"hub_sync: {ev_type} -> HTTP {status}, will retry")
            except Exception as e:
                fail += 1
                print(f"hub_sync: {ev_type} failed: {e}, will retry")

        prune_old(conn)
        print(f"hub_sync: done — {ok} sent, {fail} pending retry")
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"hub_sync: aborted safely: {e}")
    sys.exit(0)
