import unittest
from decimal import Decimal
from unittest.mock import patch

from icc_v2.data.binance_usdm import BinanceUsdmPublic


class Response:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self):
        return self.payload


class BinanceUsdmTests(unittest.TestCase):
    def setUp(self):
        self.adapter = BinanceUsdmPublic(retries=0)

    @patch("icc_v2.data.binance_usdm.urlopen")
    def test_futures_kline_preserves_reported_volume_and_taker_flow(self, mocked):
        mocked.return_value = Response(
            b'[[1000,"10","12","9","11","3.5",1999,"38.5",7,"2.1","23.1","0"]]'
        )
        candle = self.adapter.klines("btcusdt", "1m", limit=1)[0]
        self.assertEqual(candle.base_volume, 3.5)
        self.assertEqual(candle.quote_volume, 38.5)
        self.assertEqual(candle.taker_buy_base_volume, Decimal("2.1"))
        self.assertEqual(candle.taker_buy_quote_volume, Decimal("23.1"))
        self.assertEqual(candle.market_type, "perpetual")

    @patch("icc_v2.data.binance_usdm.urlopen")
    def test_only_active_perpetuals_are_discovered(self, mocked):
        mocked.return_value = Response(
            b'{"symbols":['
            b'{"symbol":"BTCUSDT","status":"TRADING","contractType":"PERPETUAL","quoteAsset":"USDT"},'
            b'{"symbol":"BTCUSD_260925","status":"TRADING","contractType":"CURRENT_QUARTER","quoteAsset":"USD"},'
            b'{"symbol":"OLDUSDT","status":"SETTLING","contractType":"PERPETUAL","quoteAsset":"USDT"}]}'
        )
        symbols = self.adapter.exchange_info(quote_assets={"USDT"})
        self.assertEqual([row["symbol"] for row in symbols], ["BTCUSDT"])

    @patch("icc_v2.data.binance_usdm.urlopen")
    def test_aggregate_trade_preserves_exchange_side_flag(self, mocked):
        mocked.return_value = Response(
            b'[{"a":9,"p":"10.5","q":"2","T":1234,"m":true}]'
        )
        trade = self.adapter.aggregate_trades("BTCUSDT", limit=1)[0]
        self.assertEqual(trade.aggregate_trade_id, 9)
        self.assertTrue(trade.buyer_is_maker)
        self.assertEqual(trade.base_quantity, 2)

    def test_rejects_bad_symbol_and_interval(self):
        with self.assertRaises(ValueError):
            self.adapter.klines("BTC/USDT")
        with self.assertRaises(ValueError):
            self.adapter.klines("BTCUSDT", "10m")


if __name__ == "__main__":
    unittest.main()
