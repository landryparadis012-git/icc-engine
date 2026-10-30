"""Integration tests for the open funnel, signal ledger and feed wiring.

Ladder 2 (module boundaries + persistence) and ladder 4 (failure behaviour):
every open path must pass risk.validate_open() inside start_focus_trade(), a
rejected open must be auditable, and a bad feed must never reach a decision.

No network: tg_send and data_layer.fetch_candles are stubbed.
"""

import os
import sqlite3
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.join(os.path.dirname(HERE), "engine")
sys.path.insert(0, ENGINE_DIR)

import engine  # noqa: E402


def pending_setup(**over):
    now = int(time.time())
    p = {
        "symbol": "XAUUSD", "tf": "15m", "direction": 1, "tier": "TAKE",
        "entry": 2650.0, "sl": 2645.0, "tp": 1.5,
        "feats": {"disp": 0, "cont": 0, "depth": 0, "sweep": 0, "vol": 50},
        "quality": 60.0, "momentum": 55.0, "conf": 3, "regime": 0,
        "session": "LDN", "behav": {},
        "created_at": now, "bar_ts": now - 300,
    }
    p.update(over)
    return p


def fresh_state(**over):
    st = {
        "focus_list": [], "pending": None, "daily_r": 0.0, "day_opens": 0,
        "pause_until": 0, "day": "2027-01-15", "loss_streak": 0, "cooldowns": {},
    }
    st.update(over)
    return st


class FunnelBase(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(engine.SIGNALS_DDL)
        self.sent = []
        self._orig_send = engine.tg_send
        engine.tg_send = lambda txt, keyboard=None, *a, **k: self.sent.append(txt)
        self._env = dict(os.environ)
        os.environ["RISK_KILL_FILE"] = os.path.join(HERE, "no-kill-file")

    def tearDown(self):
        engine.tg_send = self._orig_send
        os.environ.clear()
        os.environ.update(self._env)
        self.db.close()

    def signals(self):
        return self.db.execute(
            "SELECT status, reason FROM signals ORDER BY updated_at").fetchall()


class TestOpenFunnel(FunnelBase):
    def test_valid_setup_opens_and_is_audited(self):
        st = fresh_state()
        ok = engine.start_focus_trade(st, pending_setup(), con=self.db)
        self.assertTrue(ok)
        self.assertEqual(len(st["focus_list"]), 1)
        self.assertEqual(st["day_opens"], 1)
        rows = self.signals()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "opened")
        self.assertTrue(any("TRACKING" in m for m in self.sent))

    def test_kill_switch_blocks_every_open_path(self):
        st = fresh_state(kill_switch=True)
        ok = engine.start_focus_trade(st, pending_setup(), con=self.db)
        self.assertFalse(ok)
        self.assertEqual(st["focus_list"], [])
        self.assertEqual(st["day_opens"], 0)
        rows = self.signals()
        self.assertEqual(rows[0][0], "rejected")
        self.assertIn("kill switch", rows[0][1])
        self.assertTrue(any("RISK BLOCK" in m for m in self.sent))

    def test_duplicate_direction_is_rejected(self):
        st = fresh_state(focus_list=[{
            "symbol": "XAUUSD", "direction": 1, "entry": 2650.0, "sl": 2645.0,
            "tier": "TAKE", "tf": "15m", "tp_r": 1.5, "feats": {}, "opened_ts": 1,
        }])
        ok = engine.start_focus_trade(st, pending_setup(), con=self.db)
        self.assertFalse(ok)
        self.assertEqual(len(st["focus_list"]), 1)
        self.assertEqual(self.signals()[0][0], "rejected")

    def test_stale_card_cannot_be_opened(self):
        st = fresh_state()
        ok = engine.start_focus_trade(st, pending_setup(created_at=int(time.time()) - 7200),
                                      con=self.db)
        self.assertFalse(ok)
        self.assertIn("stale signal", self.signals()[0][1])

    def test_unverifiable_feed_blocks_the_open(self):
        st = fresh_state()
        p = pending_setup()
        p.pop("bar_ts")
        ok = engine.start_focus_trade(st, p, con=self.db)
        self.assertFalse(ok)
        self.assertIn("market data age unknown", self.signals()[0][1])

    def test_manual_entry_registers_without_card_age(self):
        st = fresh_state()
        p = pending_setup(tf="manual", source="manual", price=2650.0)
        p.pop("bar_ts")
        ok = engine.start_focus_trade(st, p, con=self.db)
        self.assertTrue(ok, self.signals())
        self.assertEqual(len(st["focus_list"]), 1)

    def test_no_connection_still_blocks(self):
        st = fresh_state(kill_switch=True)
        ok = engine.start_focus_trade(st, pending_setup(), con=None)
        self.assertFalse(ok)
        self.assertEqual(st["focus_list"], [])


class TestSignalLedger(FunnelBase):
    def test_lifecycle_updates_one_row(self):
        p = pending_setup()
        engine.record_signal(self.db, p, "pending", "alerted")
        engine.record_signal(self.db, p, "opened", "tier=TAKE")
        engine.record_signal(self.db, dict(p, tp=1.5), "closed", "+1.50R managed")
        rows = self.db.execute(
            "SELECT status, reason FROM signals").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "closed")
        self.assertIn("1.50R", rows[0][1])

    def test_signal_id_is_stable(self):
        p = pending_setup()
        self.assertEqual(engine.signal_id(p), engine.signal_id(dict(p)))

    def test_missing_connection_is_a_no_op(self):
        engine.record_signal(None, pending_setup(), "pending", "x")  # must not raise

    def test_record_survives_a_bad_payload(self):
        engine.record_signal(self.db, {"symbol": "XAUUSD"}, "pending", "partial")


class TestFeedWiring(unittest.TestCase):
    def setUp(self):
        self._orig = engine.data_layer.fetch_candles

    def tearDown(self):
        engine.data_layer.fetch_candles = self._orig

    def test_clean_feed_passes_through(self):
        now = int(time.time())
        series = [(now - 40 * 900 + i * 900, 100.0, 101.0, 99.0, 100.5) for i in range(40)]
        engine.data_layer.fetch_candles = lambda *a, **k: series
        self.assertEqual(len(engine.fetch_chart("GC=F", "15m", "1d")), 40)

    def test_stale_feed_is_refused(self):
        now = int(time.time())
        series = [(now - 200000 + i * 900, 100.0, 101.0, 99.0, 100.5) for i in range(40)]
        engine.data_layer.fetch_candles = lambda *a, **k: series
        self.assertEqual(engine.fetch_chart("GC=F", "15m", "1d"), [])

    def test_malformed_rows_are_dropped(self):
        now = int(time.time())
        series = [(now - 40 * 900 + i * 900, 100.0, 101.0, 99.0, 100.5) for i in range(40)]
        series.append(("bad", "row"))
        engine.data_layer.fetch_candles = lambda *a, **k: series
        self.assertEqual(len(engine.fetch_chart("GC=F", "15m", "1d")), 40)

    def test_provider_failure_returns_empty(self):
        def boom(*a, **k):
            raise RuntimeError("provider down")
        engine.data_layer.fetch_candles = boom
        self.assertEqual(engine.fetch_chart("GC=F", "15m", "1d"), [])


if __name__ == "__main__":
    unittest.main()
