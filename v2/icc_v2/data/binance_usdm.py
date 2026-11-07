"""Read-only Binance USD-M perpetual futures public market adapter.

Uses public REST endpoints only. Contract quantity and quote notional semantics
are venue-specific; this module preserves raw base quantity and exchange-reported
quote volume from kline records. It does not authenticate or place orders.
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


BASE_URL = "https://fapi.binance.com"


class FuturesMarketDataError(RuntimeError):
    pass


class FuturesRateLimitError(FuturesMarketDataError):
    pass


def _decimal(value: Any, field: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FuturesMarketDataError(f"Invalid decimal in {field}") from exc
    if not number.is_finite():
        raise FuturesMarketDataError(f"Non-finite decimal in {field}")
    return number


@dataclass(frozen=True)
class FuturesCandle:
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
    base_volume: Decimal
    quote_volume: Decimal
    trade_count: int
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal


@dataclass(frozen=True)
class AggregateTrade:
    venue: str
    market_type: str
    symbol: str
    aggregate_trade_id: int
    price: Decimal
    base_quantity: Decimal
    event_time_ms: int
    buyer_is_maker: bool


class BinanceUsdmPublic:
    """Public USD-M perpetual market data; no API key required."""

    venue = "binance"
    market_type = "perpetual"

    def __init__(self, *, base_url: str = BASE_URL, timeout: float = 10.0, retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urlencode(params or {})
        request = Request(
            f"{self.base_url}{path}" + (f"?{query}" if query else ""),
            headers={"User-Agent": "ICC-V2-market-data/0.1", "Accept": "application/json"},
            method="GET",
        )
        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code in (418, 429):
                    if exc.code == 418 or attempt >= self.retries:
                        raise FuturesRateLimitError(f"Binance Futures rate limit / ban: HTTP {exc.code}") from exc
                    delay = exc.headers.get("Retry-After")
                    time.sleep(min(float(delay) if delay else 2 ** attempt, 30.0))
                    continue
                if exc.code >= 500 and attempt < self.retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise FuturesMarketDataError(f"Binance Futures HTTP {exc.code} for {path}") from exc
            except (URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                if attempt >= self.retries:
                    raise FuturesMarketDataError(f"Binance Futures request failed for {path}") from exc
                time.sleep(min(2 ** attempt, 8))
        raise FuturesMarketDataError(f"Binance Futures retries exhausted for {path}")

    @staticmethod
    def _symbol(symbol: str) -> str:
        value = symbol.strip().upper()
        if not value or not value.isalnum():
            raise ValueError("symbol must be an alphanumeric exchange symbol")
        return value

    def exchange_info(self, *, quote_assets: set[str] | None = None) -> list[dict[str, Any]]:
        payload = self._get("/fapi/v1/exchangeInfo")
        rows = payload.get("symbols") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise FuturesMarketDataError("Unexpected Futures exchangeInfo response")
        quotes = {x.upper() for x in quote_assets} if quote_assets else None
        return [
            row for row in rows
            if isinstance(row, dict)
            and row.get("status") == "TRADING"
            and row.get("contractType") == "PERPETUAL"
            and (quotes is None or row.get("quoteAsset") in quotes)
        ]

    def klines(self, symbol: str, interval: str = "1m", *, limit: int = 500) -> list[FuturesCandle]:
        allowed = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
        if interval not in allowed:
            raise ValueError(f"Unsupported Futures interval: {interval}")
        if not 1 <= limit <= 1500:
            raise ValueError("limit must be between 1 and 1500")
        normalized = self._symbol(symbol)
        rows = self._get("/fapi/v1/klines", {"symbol": normalized, "interval": interval, "limit": limit})
        if not isinstance(rows, list):
            raise FuturesMarketDataError("Unexpected Futures kline response")
        candles = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 11:
                raise FuturesMarketDataError("Malformed Futures kline row")
            candles.append(FuturesCandle(
                venue=self.venue, market_type=self.market_type, symbol=normalized,
                interval=interval, open_time_ms=int(row[0]), close_time_ms=int(row[6]),
                open=_decimal(row[1], "open"), high=_decimal(row[2], "high"),
                low=_decimal(row[3], "low"), close=_decimal(row[4], "close"),
                base_volume=_decimal(row[5], "base_volume"),
                quote_volume=_decimal(row[7], "quote_volume"),
                trade_count=int(row[8]),
                taker_buy_base_volume=_decimal(row[9], "taker_buy_base_volume"),
                taker_buy_quote_volume=_decimal(row[10], "taker_buy_quote_volume"),
            ))
        return candles

    def aggregate_trades(self, symbol: str, *, limit: int = 500) -> list[AggregateTrade]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        normalized = self._symbol(symbol)
        rows = self._get("/fapi/v1/aggTrades", {"symbol": normalized, "limit": limit})
        if not isinstance(rows, list):
            raise FuturesMarketDataError("Unexpected aggregate-trade response")
        return [
            AggregateTrade(
                venue=self.venue, market_type=self.market_type, symbol=normalized,
                aggregate_trade_id=int(row["a"]), price=_decimal(row["p"], "price"),
                base_quantity=_decimal(row["q"], "quantity"),
                event_time_ms=int(row["T"]), buyer_is_maker=bool(row["m"]),
            )
            for row in rows
        ]

    def depth_snapshot(self, symbol: str, *, limit: int = 100) -> dict[str, Any]:
        if limit not in {5, 10, 20, 50, 100, 500, 1000}:
            raise ValueError("Unsupported Futures depth limit")
        normalized = self._symbol(symbol)
        payload = self._get("/fapi/v1/depth", {"symbol": normalized, "limit": limit})
        if not isinstance(payload, dict) or "lastUpdateId" not in payload:
            raise FuturesMarketDataError("Unexpected Futures depth response")
        return {
            "venue": self.venue, "market_type": self.market_type, "symbol": normalized,
            "event_time_ms": int(payload["E"]) if "E" in payload else None,
            "last_update_id": int(payload["lastUpdateId"]),
            "bids": tuple((_decimal(p, "bid.price"), _decimal(q, "bid.quantity")) for p, q in payload.get("bids", [])),
            "asks": tuple((_decimal(p, "ask.price"), _decimal(q, "ask.quantity")) for p, q in payload.get("asks", [])),
            "snapshot_only": True,
        }
