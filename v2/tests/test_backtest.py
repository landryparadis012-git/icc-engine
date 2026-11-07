import unittest

from icc_v2.strategy.core import SetupCandidate, Bar
from icc_v2.strategy.backtest import simulate_trade, summarize_outcomes


def candidate():
    return SetupCandidate(
        symbol="BTCUSDT", side="LONG", signal_time_ms=1,
        reference_price=100.0, stop_loss=98.0, take_profit=103.0,
        risk_per_unit=2.0, score=80.0, features={},
    )


class BacktestTests(unittest.TestCase):
    def test_entry_is_next_bar_open_and_target_exit(self):
        bars = [
            Bar(2, 100.5, 103.5, 100.0, 103.0),
            Bar(3, 103.0, 104.0, 102.0, 103.5),
        ]
        result = simulate_trade(candidate(), bars, fee_bps_per_side=0, slippage_bps_per_side=0)
        self.assertEqual(result.entry_price, 100.5)
        self.assertEqual(result.exit_reason, "TARGET")
        self.assertGreater(result.net_r, 0)

    def test_stop_first_when_stop_and_target_hit_same_bar(self):
        bars = [Bar(2, 100.0, 104.0, 97.0, 101.0)]
        result = simulate_trade(candidate(), bars, fee_bps_per_side=0, slippage_bps_per_side=0)
        self.assertEqual(result.exit_reason, "STOP_AMBIGUOUS_BAR")
        self.assertEqual(result.gross_r, -1.0)

    def test_costs_reduce_net_result(self):
        bars = [Bar(2, 100.0, 104.0, 99.0, 103.5)]
        free = simulate_trade(candidate(), bars, fee_bps_per_side=0, slippage_bps_per_side=0)
        paid = simulate_trade(candidate(), bars, fee_bps_per_side=5, slippage_bps_per_side=2)
        self.assertGreater(free.net_r, paid.net_r)

    def test_summary_reports_expectancy_and_drawdown(self):
        outcomes = [
            simulate_trade(candidate(), [Bar(2, 100, 104, 99, 103)], fee_bps_per_side=0, slippage_bps_per_side=0),
            simulate_trade(candidate(), [Bar(2, 100, 101, 97, 98)], fee_bps_per_side=0, slippage_bps_per_side=0),
        ]
        summary = summarize_outcomes(outcomes)
        self.assertEqual(summary["trades"], 2)
        self.assertAlmostEqual(summary["expectancy_r"], 0.25)
        self.assertGreater(summary["max_drawdown_r"], 0)

    def test_rejects_future_bars_that_precede_signal(self):
        with self.assertRaises(ValueError):
            simulate_trade(candidate(), [Bar(1, 100, 101, 99, 100)])

    def test_rejects_invalid_costs(self):
        with self.assertRaises(ValueError):
            simulate_trade(candidate(), [Bar(2, 100, 101, 99, 100)], fee_bps_per_side=-1)


if __name__ == "__main__":
    unittest.main()
