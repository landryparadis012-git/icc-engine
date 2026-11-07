import unittest

from icc_v2.data.binance_depth import BinanceSpotDepthBook, DepthSyncError


class BinanceDepthBookTests(unittest.TestCase):
    def setUp(self):
        self.book = BinanceSpotDepthBook("BTCUSDT")
        self.snapshot = {
            "lastUpdateId": 100,
            "bids": [["99", "2"], ["98", "3"]],
            "asks": [["101", "4"], ["102", "5"]],
        }

    def test_bootstrap_bridges_snapshot_and_applies_buffered_update(self):
        self.book.bootstrap(self.snapshot, [
            {"U": 99, "u": 100, "b": [["99", "9"]], "a": []},
            {"U": 101, "u": 102, "b": [["99", "0"], ["100", "1"]], "a": [["101", "0"]]},
        ])
        top = self.book.top()
        self.assertEqual(self.book.last_update_id, 102)
        self.assertEqual(top["bids"][0].price, 100)
        self.assertEqual(top["asks"][0].price, 102)

    def test_ignores_stale_update(self):
        self.book.bootstrap(self.snapshot, [
            {"U": 101, "u": 101, "b": [], "a": []}
        ])
        self.book.apply({"U": 100, "u": 100, "b": [["99", "99"]], "a": []})
        self.assertEqual(self.book.last_update_id, 101)
        self.assertEqual(self.book.bids[99], 2)

    def test_gap_invalidates_book(self):
        self.book.bootstrap(self.snapshot, [
            {"U": 101, "u": 101, "b": [], "a": []}
        ])
        with self.assertRaises(DepthSyncError):
            self.book.apply({"U": 104, "u": 105, "b": [], "a": []})
        self.assertFalse(self.book.synchronized)
        with self.assertRaises(DepthSyncError):
            self.book.top()

    def test_missing_bridge_requires_new_snapshot(self):
        with self.assertRaises(DepthSyncError):
            self.book.bootstrap(self.snapshot, [
                {"U": 103, "u": 104, "b": [], "a": []}
            ])
        self.assertFalse(self.book.synchronized)


if __name__ == "__main__":
    unittest.main()
