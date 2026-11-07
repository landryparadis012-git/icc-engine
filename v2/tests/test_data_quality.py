import unittest
from decimal import Decimal

from icc_v2.data.quality import validate_candle, validate_order_book


class DataQualityTests(unittest.TestCase):
    def test_accepts_fresh_valid_candle(self):
        result = validate_candle(
            open_price=Decimal("10"), high=Decimal("12"), low=Decimal("9"),
            close=Decimal("11"), volume=Decimal("3"), quote_volume=Decimal("33"),
            event_time_ms=1000, received_time_ms=1500, max_age_ms=5000,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.issues, ())

    def test_rejects_stale_candle(self):
        result = validate_candle(
            open_price=Decimal("10"), high=Decimal("12"), low=Decimal("9"),
            close=Decimal("11"), volume=Decimal("3"), quote_volume=None,
            event_time_ms=1000, received_time_ms=10000, max_age_ms=5000,
        )
        self.assertFalse(result.accepted)
        self.assertIn("STALE_DATA", [issue.code for issue in result.issues])

    def test_rejects_invalid_ohlc(self):
        result = validate_candle(
            open_price=Decimal("10"), high=Decimal("9"), low=Decimal("8"),
            close=Decimal("11"), volume=Decimal("3"), quote_volume=None,
            event_time_ms=1000, received_time_ms=1000, max_age_ms=5000,
        )
        self.assertFalse(result.accepted)
        self.assertIn("INVALID_OHLC_RANGE", [issue.code for issue in result.issues])

    def test_rejects_unsynchronized_or_crossed_book(self):
        result = validate_order_book(
            best_bid=Decimal("101"), best_ask=Decimal("100"),
            received_time_ms=2000, event_time_ms=1500, max_age_ms=5000,
            synchronized=False,
        )
        self.assertFalse(result.accepted)
        codes = [issue.code for issue in result.issues]
        self.assertIn("UNSYNCHRONIZED_BOOK", codes)
        self.assertIn("CROSSED_BOOK", codes)


if __name__ == "__main__":
    unittest.main()
