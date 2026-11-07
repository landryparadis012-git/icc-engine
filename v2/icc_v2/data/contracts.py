"""Provider-neutral market contracts and conservative data-quality checks."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


MarketType = Literal["spot", "perpetual", "future", "forex_spot", "equity", "index", "commodity", "cfd"]


@dataclass(frozen=True)
class Instrument:
    venue: str
    market_type: MarketType
    symbol: str
    base_asset: str | None
    quote_asset: str | None
    status: str
    price_tick: Decimal | None = None
    quantity_step: Decimal | None = None
    contract_size: Decimal | None = None


@dataclass(frozen=True)
class DataProvenance:
    provider: str
    venue: str | None
    market_type: str
    instrument: str
    event_time_ms: int | None
    received_time_ms: int
    data_kind: str
    is_exchange_observed: bool
    notes: str | None = None


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    severity: Literal["warning", "reject"]


@dataclass(frozen=True)
class QualityResult:
    accepted: bool
    issues: tuple[QualityIssue, ...]


def validate_candle(
    *,
    open_price: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
    quote_volume: Decimal | None,
    event_time_ms: int,
    received_time_ms: int,
    max_age_ms: int,
) -> QualityResult:
    """Reject impossible/stale candles; do not silently repair source data."""
    issues: list[QualityIssue] = []
    if min(open_price, high, low, close) <= 0:
        issues.append(QualityIssue("NON_POSITIVE_PRICE", "Candle prices must be positive.", "reject"))
    if high < max(open_price, close, low) or low > min(open_price, close, high):
        issues.append(QualityIssue("INVALID_OHLC_RANGE", "OHLC values violate high/low bounds.", "reject"))
    if volume < 0 or (quote_volume is not None and quote_volume < 0):
        issues.append(QualityIssue("NEGATIVE_VOLUME", "Reported volume cannot be negative.", "reject"))
    age = received_time_ms - event_time_ms
    if age < -5_000:
        issues.append(QualityIssue("FUTURE_EVENT_TIME", "Event timestamp is materially ahead of receive time.", "reject"))
    elif age > max_age_ms:
        issues.append(QualityIssue("STALE_DATA", "Candle is older than the configured freshness limit.", "reject"))
    elif age > max_age_ms * 0.8:
        issues.append(QualityIssue("AGING_DATA", "Candle is approaching the freshness limit.", "warning"))
    return QualityResult(
        accepted=not any(issue.severity == "reject" for issue in issues),
        issues=tuple(issues),
    )


def validate_order_book(
    *,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
    received_time_ms: int,
    event_time_ms: int | None,
    max_age_ms: int,
    synchronized: bool,
) -> QualityResult:
    """A book must be synchronized, fresh and non-crossed before strategy use."""
    issues: list[QualityIssue] = []
    if not synchronized:
        issues.append(QualityIssue("UNSYNCHRONIZED_BOOK", "Order book has no verified update continuity.", "reject"))
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask <= 0:
        issues.append(QualityIssue("EMPTY_BOOK", "Both sides need a positive best price.", "reject"))
    elif best_bid >= best_ask:
        issues.append(QualityIssue("CROSSED_BOOK", "Best bid must be below best ask.", "reject"))
    if event_time_ms is None:
        issues.append(QualityIssue("MISSING_EVENT_TIME", "Provider event timestamp is unavailable.", "warning"))
    else:
        age = received_time_ms - event_time_ms
        if age < -5_000:
            issues.append(QualityIssue("FUTURE_EVENT_TIME", "Book timestamp is ahead of receive time.", "reject"))
        elif age > max_age_ms:
            issues.append(QualityIssue("STALE_BOOK", "Order-book update is stale.", "reject"))
    return QualityResult(
        accepted=not any(issue.severity == "reject" for issue in issues),
        issues=tuple(issues),
    )
