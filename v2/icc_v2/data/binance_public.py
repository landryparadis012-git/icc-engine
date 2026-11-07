"""Read-only Binance Spot market-data adapter.

This module uses public REST endpoints only. It never authenticates, reads account
state, or places orders. REST order-book responses are snapshots, not a stream.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.binance.com"
_ALLOWED_INTERVALS = {
    "1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h",
    "6h", "8h", "12h", "1d", "3d", "1w", "1M",
}
_ALLOWED_DEPTH_LIMITS = {5, 10, 20, 50, 100, 500, 1000, 5000}


class MarketDataError(RuntimeError):
    """Base error for public market-data failures."""


class RateLimitError(MarketDataError):
    """The venue rejected the request due to rate limiting."""


class VenueUnavailableError(MarketDataError):
    """The venue is temporarily unavailable."""


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketDataError(f"Invalid decimal in {field}") from exc
    if not result.is_finite():
        raise MarketDataError(f"Non-finite decimal in {field}")
    return result


@dataclass(frozen=True)
class Candle:
    venue: str
    market_type: str
    symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int


@dataclass(frozen=True)
class PublicTrade:
    venue: str
    market_type: str
    symbol: str
    trade_id: int
    price: Decimal
    quantity: Decimal
    quote_quantity: Decimal
    event_time_ms: int
    buyer_is_maker: bool


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class OrderBookSnapshot:
    venue: str
    market_type: str
    symbol: str
    event_time_ms: int | None
    last_update_id: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    snapshot_only: bool = True


class BinancePublicSpot:
    """Conservative, read-only adapter for Binance public Spot REST data."""

    venue = "binance"
    market_type = "spot"

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        retries: int = 3,
        user_agent: str = "ICC-V2-market-data/0.1",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)
        self.user_agent = user_agent

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urlencode(params or {})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        request = Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
            method="GET",
        )
        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                try:
                    return json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise MarketDataError(f"Malformed JSON from {path}") from exc
            except HTTPError as exc:
                if exc.code in (418, 429):
                    if exc.code == 418:
                        raise RateLimitError("Binance IP ban response (418); stop requests") from exc
                    if attempt >= self.retries:
                        raise RateLimitError("Binance rate limit reached (429)") from exc
                    retry_after = exc.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else min(2 ** attempt, 8)
                    time.sleep(max(0.0, min(delay, 30.0)))
                    continue
                if exc.code >= 500 and attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise MarketDataError(f"Binance HTTP {exc.code} for {path}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.retries:
                    raise VenueUnavailableError(f"Binance request failed for {path}") from exc
                time.sleep(min(2 ** attempt, 8))
        raise VenueUnavailableError(f"Binance request exhausted retries for {path}")

    @staticmethod
    def _symbol(symbol: str) -> str:
        normalized = symbol.strip().upper()
        if not normalized or not normalized.isalnum():
            raise ValueError("symbol must be an alphanumeric exchange symbol, e.g. BTCUSDT")
        return normalized

    def exchange_info(self, *, quote_assets: set[str] | None = None) -> list[dict[str, Any]]:
        """Return active Spot symbols, optionally filtered by quote asset."""
        payload = self._get("/api/v3/exchangeInfo")
        symbols = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(symbols, list):
            raise MarketDataError("Unexpected exchangeInfo response")
        allowed_quotes = {q.upper() for q in quote_assets} if quote_assets else None
        result = []
        for item in symbols:
            if not isinstance(item, dict) or item.get("status") != "TRADING":
                continue
            if item.get("isSpotTradingAllowed") is False:
                continue
            if allowed_quotes and item.get("quoteAsset") not in allowed_quotes:
                continue
            result.append(item)
        return result

    def ticker_24h(self, symbol: str) -> dict[str, Any]:
        """Return 24-hour statistics; this is not a trade-by-trade feed."""
        payload = self._get("/api/v3/ticker/24hr", {"symbol": self._symbol(symbol)})
        if not isinstance(payload, dict):
            raise MarketDataError("Unexpected 24-hour ticker response")
        return {
            "venue": self.venue,
            "market_type": self.market_type,
            "symbol": payload.get("symbol"),
            "event_time_ms": int(payload["closeTime"]) if "closeTime" in payload else None,
            "last_price": _decimal(payload["lastPrice"], "lastPrice"),
            "base_volume_24h": _decimal(payload["volume"], "volume"),
            "quote_volume_24h": _decimal(payload["quoteVolume"], "quoteVolume"),
            "trade_count_24h": int(payload["count"]),
            "price_change_percent_24h": _decimal(payload["priceChangePercent"], "priceChangePercent"),
        }

    def klines(self, symbol: str, interval: str = "1m", *, limit: int = 500) -> list[Candle]:
        """Return candles with exchange-reported base and quote volume."""
        if interval not in _ALLOWED_INTERVALS:
            raise ValueError(f"Unsupported interval: {interval}")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        payload = self._get(
            "/api/v3/klines",
            {"symbol": self._symbol(symbol), "interval": interval, "limit": limit},
        )
        if not isinstance(payload, list):
            raise MarketDataError("Unexpected klines response")
        candles: list[Candle] = []
        for row in payload:
            if not isinstance(row, list) or len(row) < 11:
                raise MarketDataError("Malformed kline row")
            candles.append(Candle(
                venue=self.venue,
                market_type=self.market_type,
                symbol=self._symbol(symbol),
                interval=interval,
                open_time_ms=int(row[0]),
                close_time_ms=int(row[6]),
                open=_decimal(row[1], "open"),
                high=_decimal(row[2], "high"),
                low=_decimal(row[3], "low"),
                close=_decimal(row[4], "close"),
                volume=_decimal(row[5], "volume"),
                quote_volume=_decimal(row[7], "quote_volume"),
                trade_count=int(row[8]),
            ))
        return candles

    def recent_trades(self, symbol: str, *, limit: int = 500) -> list[PublicTrade]:
        """Return recent public trades; historical pagination is not implemented."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        normalized = self._symbol(symbol)
        payload = self._get("/api/v3/trades", {"symbol": normalized, "limit": limit})
        if not isinstance(payload, list):
            raise MarketDataError("Unexpected recent trades response")
        return [
            PublicTrade(
                venue=self.venue,
                market_type=self.market_type,
                symbol=normalized,
                trade_id=int(row["id"]),
                price=_decimal(row["price"], "trade.price"),
                quantity=_decimal(row["qty"], "trade.qty"),
                quote_quantity=_decimal(row["quoteQty"], "trade.quoteQty"),
                event_time_ms=int(row["time"]),
                buyer_is_maker=bool(row["isBuyerMaker"]),
            )
            for row in payload
        ]

    def order_book(self, symbol: str, *, limit: int = 100) -> OrderBookSnapshot:
        """Return a point-in-time REST depth snapshot, not a live synchronized book."""
        if limit not in _ALLOWED_DEPTH_LIMITS:
            raise ValueError(f"limit must be one of {sorted(_ALLOWED_DEPTH_LIMITS)}")
        normalized = self._symbol(symbol)
        payload = self._get("/api/v3/depth", {"symbol": normalized, "limit": limit})
        if not isinstance(payload, dict) or "lastUpdateId" not in payload:
            raise MarketDataError("Unexpected order-book response")
        return OrderBookSnapshot(
            venue=self.venue,
            market_type=self.market_type,
            symbol=normalized,
            event_time_ms=int(payload["E"]) if "E" in payload else None,
            last_update_id=int(payload["lastUpdateId"]),
            bids=tuple(BookLevel(_decimal(p, "bid.price"), _decimal(q, "bid.quantity"))
                       for p, q in payload.get("bids", [])),
            asks=tuple(BookLevel(_decimal(p, "ask.price"), _decimal(q, "ask.quantity"))
                       for p, q in payload.get("asks", [])),
        )
