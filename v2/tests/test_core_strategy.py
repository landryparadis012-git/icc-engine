import unittest
from icc_v2.strategy.core import Bar, analyze_closed_bars, atr, ema, rsi


def make_bars(start, count, drift, *, volume=100.0, final_shape=None):
    rows = []
    price = start
    for i in range(count):
        previous = price
        price += drift
        high = max(previous, price) + 0.4
        low = min(previous, price) - 0.4
        op, close = previous, price
        if final_shape and i == count - 1:
            op, high, low, close = final_shape
        rows.append(Bar(
            timestamp_ms=(i + 1) * 60_000,
            open=op, high=high, low=low, close=close,
            volume=volume + (i % 5), quote_volume=(volume + (i % 5)) * close,
        ))
    return rows


class CoreStrategyTests(unittest.TestCase):
    def test_indicator_helpers(self):
        values = [float(i) for i in range(1, 61)]
        self.assertGreater(ema(values, 20), ema(values, 50))
        self.assertGreater(atr(make_bars(100, 60, 0.5)), 0)
        self.assertGreater(rsi(values), 50)

    def test_generates_long_candidate_with_uptrend_and_sweep(self):
        # Build a rising history, then a final bar that sweeps the recent low
        # and closes strongly bullish back above it.
        m15 = make_bars(100, 60, 0.5)
        swing_low = min(b.low for b in m15[-33:-3])
        prev = m15[-1]
        m15[-1] = Bar(prev.timestamp_ms, swing_low + 0.2, prev.close + 1.0,
                      swing_low - 0.5, prev.close + 0.9, 180.0, 180.0 * (prev.close + 0.9))
        h1 = make_bars(100, 60, 0.5)
        d1 = make_bars(100, 60, 0.5)
        candidate = analyze_closed_bars("btcusdt", m15, h1, d1)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.side, "LONG")
        self.assertGreater(candidate.take_profit, candidate.reference_price)
        self.assertGreater(candidate.features["volume_ratio"], 1.0)

    def test_flat_market_does_not_generate_candidate(self):
        flat = make_bars(100, 60, 0.0)
        self.assertIsNone(analyze_closed_bars("BTCUSDT", flat, flat, flat))

    def test_missing_volume_is_not_fabricated(self):
        m15 = make_bars(100, 60, 0.5)
        h1 = make_bars(100, 60, 0.5)
        d1 = make_bars(100, 60, 0.5)
        prev = m15[-1]
        m15[-1] = Bar(prev.timestamp_ms, prev.open, prev.high, prev.low, prev.close, None, None)
        # This bar may or may not qualify as a setup; if it does, volume is explicit.
        candidate = analyze_closed_bars("BTCUSDT", m15, h1, d1)
        if candidate:
            self.assertEqual(candidate.features["volume_ratio"], "unavailable")

    def test_invalid_target_rejected(self):
        with self.assertRaises(ValueError):
            analyze_closed_bars("BTCUSDT", [], [], [], target_r=0)


if __name__ == "__main__":
    unittest.main()
