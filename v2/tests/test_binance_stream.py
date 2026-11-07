import json
import unittest
from decimal import Decimal
from icc_v2.data.binance_stream import BinanceSpotWebSocket, StreamError
from icc_v2.data.binance_public import OrderBookSnapshot, BookLevel

class Sock:
    def __init__(self, rows): self.rows, self.closed = iter(rows), False
    def recv(self):
        try: return next(self.rows)
        except StopIteration: return None
    def close(self): self.closed = True

class Rest:
    def order_book(self, symbol, *, limit):
        return OrderBookSnapshot("binance","spot",symbol,1000,100,
            (BookLevel(Decimal("99"),Decimal("2")),),
            (BookLevel(Decimal("101"),Decimal("3")),))

class Tests(unittest.TestCase):
    def test_url_contains_trade_and_depth(self):
        s=BinanceSpotWebSocket(["BTCUSDT","ethusdt"],rest=Rest())
        self.assertIn("btcusdt@trade",s.stream_url)
        self.assertIn("ethusdt@depth@100ms",s.stream_url)

    def test_trade_deduplication(self):
        s=BinanceSpotWebSocket(["BTCUSDT"],rest=Rest(),clock_ms=lambda:2000)
        d={"e":"trade","s":"BTCUSDT","t":7,"T":1500,"p":"100","q":"0.2","m":False}
        sock=Sock([json.dumps({"data":d}),json.dumps({"data":d})])
        s.socket_factory=lambda url:sock
        events=[]
        s.run_once(events.append,max_messages=2)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0].payload["aggressor_side"],"buy")
        self.assertTrue(sock.closed)

    def test_depth_emitted_after_snapshot_bridge(self):
        s=BinanceSpotWebSocket(["BTCUSDT"],rest=Rest(),clock_ms=lambda:2000)
        d={"e":"depthUpdate","s":"BTCUSDT","E":1500,"U":101,"u":101,"b":[["99","4"]],"a":[]}
        s.socket_factory=lambda url:Sock([json.dumps({"data":d})])
        events=[]
        s.run_once(events.append,max_messages=1)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0].kind,"depth")
        self.assertTrue(events[0].payload["synchronized"])

    def test_bad_json_rejected(self):
        with self.assertRaises(StreamError):
            BinanceSpotWebSocket._decode("{bad")

if __name__=="__main__": unittest.main()
