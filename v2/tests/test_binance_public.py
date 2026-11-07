import unittest
from unittest.mock import patch

from icc_v2.data.binance_public import BinancePublicSpot, MarketDataError


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class BinancePublicAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = BinancePublicSpot(retries=0)

    @patch("icc_v2.data.binance_public.urlopen")
    def test_kline_preserves_real_base_and_quote_volume(self, mocked):
        mocked.return_value = _Response(
            b'[[1000,"10","12","9","11","3.5",1999,"38.5",7,"2","22","0"]]'
        )
        candles = self.adapter.klines("btcusdt", "1m", limit=1)
        self.assertEqual(candles[0].volume, 3.5)
        self.assertEqual(candles[0].quote_volume, 38.5)
        self.assertEqual(candles[0].trade_count, 7)
        self.assertEqual(candles[0].symbol, "BTCUSDT")

    @patch("icc_v2.data.binance_public.urlopen")
    def test_order_book_is_explicitly_snapshot_only(self, mocked):
        mocked.return_value = _Response(
            b'{"lastUpdateId":123,"bids":[["10","2"]],"asks":[["11","3"]]}'
        )
        book = self.adapter.order_book("BTCUSDT", limit=5)
        self.assertTrue(book.snapshot_only)
        self.assertEqual(book.last_update_id, 123)
        self.assertEqual(book.bids[0].quantity, 2)

    @patch("icc_v2.data.binance_public.urlopen")
    def test_recent_trades_preserve_exchange_trade_metadata(self, mocked):
        mocked.return_value = _Response(
            b'[{"id":9,"price":"10.5","qty":"2","quoteQty":"21",'
            b'"time":1234,"isBuyerMaker":true}]'
        )
        trades = self.adapter.recent_trades("BTCUSDT", limit=1)
        self.assertEqual(trades[0].trade_id, 9)
        self.assertTrue(trades[0].buyer_is_maker)
        self.assertEqual(trades[0].quote_quantity, 21)

    def test_rejects_invalid_symbol(self):
        with self.assertRaises(ValueError):
            self.adapter.klines("BTC/USDT")

    def test_rejects_unsupported_interval(self):
        with self.assertRaises(ValueError):
            self.adapter.klines("BTCUSDT", "10m")


if __name__ == "__main__":
    unittest.main()
