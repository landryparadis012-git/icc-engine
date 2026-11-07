"""Read-only Binance Spot WebSocket stream consumer.

Requires optional websocket-client at runtime. No credentials or order endpoints.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import json, time
from typing import Any, Callable
from urllib.parse import quote
from .binance_depth import BinanceSpotDepthBook, DepthSyncError
from .binance_public import BinancePublicSpot

class StreamError(RuntimeError):
    pass

@dataclass(frozen=True)
class StreamEvent:
    kind: str
    symbol: str
    event_time_ms: int | None
    received_time_ms: int
    payload: dict[str, Any]

class BinanceSpotWebSocket:
    """Bounded combined trade/depth feed with REST bootstrap and reconnect support."""
    def __init__(self, symbols: list[str], *, rest=None, socket_factory=None,
                 max_buffered_depth_events=20000, reconnect_delay=2.0,
                 max_reconnect_delay=30.0, sleep=time.sleep, clock_ms=None):
        self.symbols = sorted({self._symbol(s) for s in symbols})
        if not self.symbols:
            raise ValueError("at least one symbol is required")
        if max_buffered_depth_events < 10:
            raise ValueError("buffer limit must be at least 10")
        self.rest = rest or BinancePublicSpot()
        self.socket_factory = socket_factory
        self.max_buffered_depth_events = max_buffered_depth_events
        self.reconnect_delay = max(0.1, reconnect_delay)
        self.max_reconnect_delay = max(self.reconnect_delay, max_reconnect_delay)
        self.sleep = sleep
        self.clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self.books = {s: BinanceSpotDepthBook(s) for s in self.symbols}
        self.last_trade_ids = {}

    @staticmethod
    def _symbol(value):
        value = value.strip().lower()
        if not value or not value.isalnum():
            raise ValueError("symbols must be alphanumeric")
        return value

    @property
    def stream_url(self):
        streams = []
        for s in self.symbols:
            streams += [f"{s}@trade", f"{s}@depth@100ms"]
        return "wss://stream.binance.com:9443/stream?streams=" + quote("/".join(streams), safe="@/")

    def _connect(self):
        if self.socket_factory:
            return self.socket_factory(self.stream_url)
        try:
            import websocket
        except ImportError as exc:
            raise StreamError("Install websocket-client to use live streams") from exc
        return websocket.create_connection(self.stream_url, timeout=15, enable_multithread=True)

    @staticmethod
    def _decode(raw):
        try:
            msg = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise StreamError("Malformed combined-stream JSON") from exc
        if not isinstance(msg, dict) or not isinstance(msg.get("data"), dict):
            raise StreamError("Unexpected combined-stream payload")
        return msg["data"]

    def _snapshot(self, symbol):
        snap = self.rest.order_book(symbol, limit=1000)
        return {"lastUpdateId": snap.last_update_id,
                "bids": [[str(x.price), str(x.quantity)] for x in snap.bids],
                "asks": [[str(x.price), str(x.quantity)] for x in snap.asks]}

    def _handle(self, data, received, buffers, snapshots):
        symbol = str(data.get("s", "")).lower()
        if symbol not in self.books:
            return None
        if data.get("e") == "trade":
            trade_id = int(data["t"])
            if trade_id <= self.last_trade_ids.get(symbol, -1):
                return None
            self.last_trade_ids[symbol] = trade_id
            maker = bool(data["m"])
            return StreamEvent("trade", symbol.upper(), int(data["T"]), received, {
                "trade_id": trade_id, "price": str(data["p"]), "quantity": str(data["q"]),
                "buyer_is_maker": maker, "aggressor_side": "sell" if maker else "buy",
                "venue": "binance", "market_type": "spot"})
        if data.get("e") != "depthUpdate":
            return None
        event = {"U": data["U"], "u": data["u"], "b": data.get("b", []), "a": data.get("a", [])}
        book = self.books[symbol]
        if not book.synchronized:
            buffers[symbol].append(event)
            if len(buffers[symbol]) > self.max_buffered_depth_events:
                buffers[symbol].clear()
                snapshots[symbol] = None
                raise StreamError("Depth bootstrap buffer overflow")
            if snapshots[symbol] is None:
                snapshots[symbol] = self._snapshot(symbol)
            try:
                book.bootstrap(snapshots[symbol], list(buffers[symbol]))
            except DepthSyncError:
                return None
            buffers[symbol].clear()
        else:
            book.apply(event)
        top = book.top(20)
        return StreamEvent("depth", symbol.upper(), int(data["E"]) if "E" in data else None,
                           received, {"last_update_id": top["last_update_id"],
                           "bids": [[str(x.price), str(x.quantity)] for x in top["bids"]],
                           "asks": [[str(x.price), str(x.quantity)] for x in top["asks"]],
                           "synchronized": True, "venue": "binance", "market_type": "spot"})

    def run_once(self, on_event: Callable[[StreamEvent], None], *, max_messages=None):
        """Consume one session. On failure, caller should reconnect and resnapshot."""
        sock = self._connect()
        buffers = {s: deque() for s in self.symbols}
        snapshots = {s: None for s in self.symbols}
        count = 0
        try:
            while max_messages is None or count < max_messages:
                raw = sock.recv()
                if raw in (None, ""):
                    raise StreamError("WebSocket closed")
                event = self._handle(self._decode(raw), self.clock_ms(), buffers, snapshots)
                count += 1
                if event is not None:
                    on_event(event)
            return count
        finally:
            for book in self.books.values():
                book.invalidate()
            try:
                sock.close()
            except Exception:
                pass

    def run_forever(self, on_event, *, stop=None):
        """Reconnect with bounded exponential backoff; stop is checked between sessions."""
        delay = self.reconnect_delay
        while stop is None or not stop():
            try:
                self.run_once(on_event)
                delay = self.reconnect_delay
            except Exception:
                if stop is not None and stop():
                    return
                self.sleep(delay)
                delay = min(delay * 2, self.max_reconnect_delay)
