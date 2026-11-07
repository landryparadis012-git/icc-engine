"""Sequence-aware Binance Spot diff-depth book reconstruction primitives.

The caller owns the WebSocket connection and REST snapshot request. Buffer stream
events while obtaining a snapshot, then pass the snapshot and buffered events to
bootstrap(). Thereafter pass each diff-depth event to apply(). Any sequence gap
invalidates the book; callers must fetch a fresh snapshot and bootstrap again.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


class DepthSyncError(RuntimeError):
    """Depth updates cannot be safely applied to the current local book."""


def _d(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DepthSyncError("Invalid depth price or quantity") from exc
    if not result.is_finite() or result < 0:
        raise DepthSyncError("Depth values must be finite and non-negative")
    return result


@dataclass(frozen=True)
class DepthLevel:
    price: Decimal
    quantity: Decimal


class BinanceSpotDepthBook:
    """A single-symbol local book reconstructed from REST + diff-depth events."""

    def __init__(self, symbol: str, *, max_levels: int = 5000) -> None:
        self.symbol = symbol.strip().upper()
        if not self.symbol or not self.symbol.isalnum():
            raise ValueError("symbol must be an alphanumeric exchange symbol")
        if max_levels < 1:
            raise ValueError("max_levels must be positive")
        self.max_levels = max_levels
        self.last_update_id: int | None = None
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.synchronized = False

    @staticmethod
    def _parse_side(rows: Iterable[list[Any]], *, keep_zero: bool = False) -> dict[Decimal, Decimal]:
        side: dict[Decimal, Decimal] = {}
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise DepthSyncError("Malformed depth level")
            price, quantity = _d(row[0]), _d(row[1])
            if price <= 0:
                raise DepthSyncError("Depth price must be positive")
            if quantity > 0 or keep_zero:
                side[price] = quantity
        return side

    def bootstrap(self, snapshot: dict[str, Any], buffered_events: Iterable[dict[str, Any]]) -> None:
        """Install a REST snapshot and bridge buffered events without a sequence gap."""
        try:
            snapshot_id = int(snapshot["lastUpdateId"])
            bids = self._parse_side(snapshot["bids"])
            asks = self._parse_side(snapshot["asks"])
        except (KeyError, TypeError, ValueError) as exc:
            self.invalidate()
            raise DepthSyncError("Malformed REST depth snapshot") from exc

        events = sorted(list(buffered_events), key=lambda e: int(e.get("U", -1)))
        bridge_index = None
        target = snapshot_id + 1
        for index, event in enumerate(events):
            try:
                first_id, final_id = int(event["U"]), int(event["u"])
            except (KeyError, TypeError, ValueError) as exc:
                self.invalidate()
                raise DepthSyncError("Malformed buffered depth event") from exc
            if final_id < target:
                continue
            if first_id <= target <= final_id:
                bridge_index = index
                break
            if first_id > target:
                self.invalidate()
                raise DepthSyncError("Buffered depth stream begins after snapshot; resnapshot required")

        if bridge_index is None:
            # No event bridged the snapshot. Keep it explicitly unsynchronized.
            self.invalidate()
            raise DepthSyncError("No buffered event bridges the REST snapshot")

        self.bids, self.asks = bids, asks
        self.last_update_id = snapshot_id
        self.synchronized = True
        for event in events[bridge_index:]:
            self.apply(event)
        self._trim()

    def apply(self, event: dict[str, Any]) -> None:
        """Apply one diff event; reject duplicates and invalidate on a sequence gap."""
        if not self.synchronized or self.last_update_id is None:
            raise DepthSyncError("Book is not synchronized")
        try:
            first_id, final_id = int(event["U"]), int(event["u"])
            bids = self._parse_side(event.get("b", []), keep_zero=True)
            asks = self._parse_side(event.get("a", []), keep_zero=True)
        except (KeyError, TypeError, ValueError) as exc:
            self.invalidate()
            raise DepthSyncError("Malformed depth update") from exc

        if final_id <= self.last_update_id:
            return  # stale/duplicate update
        if first_id > self.last_update_id + 1:
            self.invalidate()
            raise DepthSyncError("Depth update sequence gap; discard book and resnapshot")
        if final_id < self.last_update_id + 1:
            return

        for price, quantity in bids.items():
            if quantity == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = quantity
        for price, quantity in asks.items():
            if quantity == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = quantity
        self.last_update_id = final_id
        self._trim()

    def _trim(self) -> None:
        bid_prices = sorted(self.bids, reverse=True)
        ask_prices = sorted(self.asks)
        self.bids = {p: self.bids[p] for p in bid_prices[:self.max_levels]}
        self.asks = {p: self.asks[p] for p in ask_prices[:self.max_levels]}

    def invalidate(self) -> None:
        self.synchronized = False
        self.last_update_id = None
        self.bids.clear()
        self.asks.clear()

    def top(self, depth: int = 10) -> dict[str, Any]:
        if not self.synchronized:
            raise DepthSyncError("Book is not synchronized")
        if depth < 1:
            raise ValueError("depth must be positive")
        bid_rows = sorted(self.bids.items(), reverse=True)[:depth]
        ask_rows = sorted(self.asks.items())[:depth]
        return {
            "symbol": self.symbol,
            "last_update_id": self.last_update_id,
            "bids": tuple(DepthLevel(p, q) for p, q in bid_rows),
            "asks": tuple(DepthLevel(p, q) for p, q in ask_rows),
            "synchronized": True,
        }
