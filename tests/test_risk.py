"""Risk firewall unit tests (Phase 5H, ladder 1 + 4).

Covers the checks in risk.validate_open() and the feed guards in
risk.validate_bars(): limits, fail-closed behaviour, duplicate/hedge detection,
staleness, stop validity, target envelope and malformed market data.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_DIR = os.path.join(os.path.dirname(HERE), "engine")
sys.path.insert(0, ENGINE_DIR)

import risk  # noqa: E402

NOW = 1_800_000_000


def limits(**over):
    lim = risk.limits()
    lim["kill_file"] = os.path.join(HERE, "no-such-kill-file")
    lim.update(over)
    return lim


def pending(**over):
    """A setup that passes every check by default."""
    p = {
        "symbol": "XAUUSD",
        "tf": "15m",
        "direction": 1,
        "entry": 2650.0,
        "sl": 2645.0,
        "tp": 1.5,
        "created_at": NOW - 60,
        "bar_ts": NOW - 300,
    }
    p.update(over)
    return p


def state(**over):
    st = {
        "focus_list": [],
        "day": "2027-01-15",
        "daily_r": 0.0,
        "day_opens": 0,
        "pause_until": 0,
    }
    st.update(over)
    return st


def open_trade(symbol="US30", direction=1, **over):
    t = {"symbol": symbol, "direction": direction, "entry": 100.0, "sl": 99.0}
    t.update(over)
    return t


def run(p, st=None, **lim_over):
    return risk.validate_open(state() if st is None else st, p, now=NOW, lim=limits(**lim_over))


class TestHappyPath(unittest.TestCase):
    def test_clean_setup_is_allowed(self):
        ok, reasons = run(pending())
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])

    def test_firewall_never_mutates_the_setup(self):
        p = pending()
        before = dict(p)
        risk.validate_open(state(), p, now=NOW, lim=limits())
        self.assertEqual(p, before)


class TestSessionStops(unittest.TestCase):
    def test_kill_switch_in_state(self):
        ok, reasons = run(pending(), state(kill_switch=True))
        self.assertFalse(ok)
        self.assertTrue(any("kill switch" in r for r in reasons))

    def test_kill_file_on_disk(self):
        path = os.path.join(HERE, "KILL.test")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("stop\n")
        try:
            ok, reasons = run(pending(), kill_file=path)
            self.assertFalse(ok)
            self.assertTrue(any("kill file" in r for r in reasons))
        finally:
            os.remove(path)

    def test_loss_streak_pause(self):
        ok, reasons = run(pending(), state(pause_until=NOW + 600))
        self.assertFalse(ok)
        self.assertIn("loss-streak pause active", reasons)

    def test_pause_expired_is_allowed(self):
        ok, _ = run(pending(), state(pause_until=NOW - 1))
        self.assertTrue(ok)

    def test_daily_loss_lockout(self):
        ok, reasons = run(pending(), state(daily_r=-3.0))
        self.assertFalse(ok)
        self.assertIn("daily loss limit reached", reasons)

    def test_daily_loss_above_limit_is_allowed(self):
        ok, _ = run(pending(), state(daily_r=-2.99))
        self.assertTrue(ok)


class TestPositionLimits(unittest.TestCase):
    def test_max_concurrent(self):
        st = state(focus_list=[open_trade("US30"), open_trade("BTC", 1)])
        ok, reasons = run(pending(), st)
        self.assertFalse(ok)
        self.assertTrue(any("max concurrent" in r for r in reasons))

    def test_duplicate_same_direction(self):
        st = state(focus_list=[open_trade("XAUUSD", 1)])
        ok, reasons = run(pending(), st)
        self.assertFalse(ok)
        self.assertTrue(any("duplicate position" in r for r in reasons))

    def test_opposite_direction_is_a_hedge_and_blocked(self):
        st = state(focus_list=[open_trade("XAUUSD", -1)])
        ok, reasons = run(pending(), st)
        self.assertFalse(ok)
        self.assertTrue(any("hedging off" in r for r in reasons))

    def test_daily_open_cap(self):
        ok, reasons = run(pending(), state(day_opens=6))
        self.assertFalse(ok)
        self.assertTrue(any("daily open cap" in r for r in reasons))

    def test_daily_open_cap_is_configurable(self):
        ok, reasons = run(pending(), state(day_opens=2), max_opens_day=2)
        self.assertFalse(ok)
        self.assertTrue(any("daily open cap" in r for r in reasons))

    def test_env_override_is_honoured(self):
        os.environ["RISK_MAX_OPENS_DAY"] = "1"
        try:
            self.assertEqual(risk.limits()["max_opens_day"], 1)
        finally:
            del os.environ["RISK_MAX_OPENS_DAY"]


class TestStaleness(unittest.TestCase):
    def test_stale_card_rejected(self):
        ok, reasons = run(pending(created_at=NOW - 3600))
        self.assertFalse(ok)
        self.assertTrue(any("stale signal" in r for r in reasons))

    def test_unknown_card_age_fails_closed(self):
        p = pending()
        p.pop("created_at")
        ok, reasons = run(p)
        self.assertFalse(ok)
        self.assertTrue(any("age unknown" in r for r in reasons))

    def test_unknown_age_can_be_explicitly_allowed(self):
        p = pending()
        p.pop("created_at")
        p.pop("bar_ts")
        ok, _ = run(p, allow_unknown_age=True)
        self.assertTrue(ok)

    def test_stale_market_data_rejected(self):
        ok, reasons = run(pending(bar_ts=NOW - 4000))
        self.assertFalse(ok)
        self.assertTrue(any("stale market data" in r for r in reasons))

    def test_unknown_bar_time_fails_closed(self):
        p = pending()
        p.pop("bar_ts")
        ok, reasons = run(p)
        self.assertFalse(ok)
        self.assertTrue(any("market data age unknown" in r for r in reasons))

    def test_daily_timeframe_uses_a_wider_cap(self):
        ok, reasons = run(pending(tf="1d", bar_ts=NOW - 20 * 3600))
        self.assertTrue(ok, reasons)


class TestStopAndTarget(unittest.TestCase):
    def test_stop_missing(self):
        p = pending()
        p.pop("sl")
        ok, reasons = run(p)
        self.assertFalse(ok)
        self.assertIn("stop loss missing", reasons)

    def test_stop_on_wrong_side_of_buy(self):
        ok, reasons = run(pending(sl=2655.0))
        self.assertFalse(ok)
        self.assertTrue(any("below entry for a BUY" in r for r in reasons))

    def test_stop_on_wrong_side_of_sell(self):
        ok, reasons = run(pending(direction=-1, sl=2640.0))
        self.assertFalse(ok)
        self.assertTrue(any("above entry for a SELL" in r for r in reasons))

    def test_valid_sell_passes(self):
        ok, reasons = run(pending(direction=-1, sl=2655.0))
        self.assertTrue(ok, reasons)

    def test_zero_risk(self):
        ok, reasons = run(pending(sl=2650.0))
        self.assertFalse(ok)
        self.assertTrue(any("zero risk" in r for r in reasons))

    def test_invalid_entry(self):
        ok, reasons = run(pending(entry=0))
        self.assertFalse(ok)
        self.assertIn("invalid entry price", reasons)

    def test_target_outside_envelope(self):
        ok, reasons = run(pending(tp=0.5))
        self.assertFalse(ok)
        self.assertTrue(any("outside configured envelope" in r for r in reasons))

    def test_target_missing(self):
        p = pending()
        p.pop("tp")
        ok, reasons = run(p)
        self.assertFalse(ok)
        self.assertIn("target missing", reasons)


class TestManualEntries(unittest.TestCase):
    """Owner-logged positions are structural-checked, not age-checked."""

    def test_manual_entry_without_card_or_feed_age_passes(self):
        ok, reasons = run(pending(source="manual", tf="manual"))
        self.assertTrue(ok, reasons)

    def test_manual_entry_still_needs_a_valid_stop(self):
        ok, reasons = run(pending(source="manual", tf="manual", sl=2660.0))
        self.assertFalse(ok)
        self.assertTrue(any("below entry for a BUY" in r for r in reasons))

    def test_manual_entry_still_respects_concurrency(self):
        st = state(focus_list=[open_trade("US30"), open_trade("BTC")])
        ok, reasons = run(pending(source="manual", tf="manual"), st)
        self.assertFalse(ok)
        self.assertTrue(any("max concurrent" in r for r in reasons))


class TestFeedValidation(unittest.TestCase):
    def bars(self, n=40, step=900, start=None, last=None):
        start = NOW - step * n if start is None else start
        out = [(start + i * step, 100.0, 101.0, 99.0, 100.5) for i in range(n)]
        if last is not None:
            out[-1] = last
        return out

    def test_clean_series(self):
        clean, fatal, dropped = risk.validate_bars(self.bars(), "15m", now=NOW, lim=limits())
        self.assertEqual(fatal, [])
        self.assertEqual(dropped, [])
        self.assertEqual(len(clean), 40)

    def test_malformed_row_is_dropped_not_fatal(self):
        series = self.bars() + [("nope", 1, 2, 3, 4)]
        clean, fatal, dropped = risk.validate_bars(series, "15m", now=NOW, lim=limits())
        self.assertEqual(fatal, [])
        self.assertEqual(len(dropped), 1)
        self.assertEqual(len(clean), 40)

    def test_duplicate_timestamp_is_dropped(self):
        series = self.bars()
        series.insert(10, series[9])
        clean, fatal, dropped = risk.validate_bars(series, "15m", now=NOW, lim=limits())
        self.assertEqual(fatal, [])
        self.assertEqual(dropped, ["duplicate timestamp"])
        self.assertEqual(len(clean), 40)

    def test_non_monotonic_timestamps_are_fatal(self):
        series = self.bars()
        series[5], series[6] = series[6], series[5]
        _, fatal, _ = risk.validate_bars(series, "15m", now=NOW, lim=limits())
        self.assertTrue(any("non-monotonic" in f for f in fatal))

    def test_future_bar_is_fatal(self):
        series = self.bars()
        series[-1] = (NOW + 3600, 100.0, 101.0, 99.0, 100.5)
        _, fatal, _ = risk.validate_bars(series, "15m", now=NOW, lim=limits())
        self.assertTrue(any("future" in f for f in fatal))

    def test_stale_feed_is_fatal(self):
        series = self.bars(start=NOW - 86400 * 2)
        _, fatal, _ = risk.validate_bars(series, "15m", now=NOW, lim=limits())
        self.assertTrue(any("stale feed" in f for f in fatal))

    def test_inconsistent_ohlc_is_dropped(self):
        series = self.bars()
        series[-1] = (series[-1][0], 100.0, 98.0, 99.0, 100.5)
        clean, fatal, dropped = risk.validate_bars(series, "15m", now=NOW, lim=limits())
        self.assertEqual(fatal, [])
        self.assertEqual(dropped, ["OHLC inconsistent"])
        self.assertEqual(len(clean), 39)

    def test_too_few_bars_is_fatal(self):
        _, fatal, _ = risk.validate_bars([(NOW - 60, 1, 1, 1, 1)], "15m", now=NOW, lim=limits())
        self.assertTrue(any("fewer than 2" in f for f in fatal))

    def test_empty_feed_is_fatal(self):
        clean, fatal, dropped = risk.validate_bars([], "15m", now=NOW, lim=limits())
        self.assertEqual(clean, [])
        self.assertTrue(fatal)

    def test_is_stale(self):
        self.assertTrue(risk.is_stale(self.bars(start=NOW - 86400 * 2), "15m", now=NOW, lim=limits()))
        self.assertFalse(risk.is_stale(self.bars(), "15m", now=NOW, lim=limits()))


if __name__ == "__main__":
    unittest.main()
