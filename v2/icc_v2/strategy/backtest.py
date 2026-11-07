"""Conservative OHLC backtest primitives for ICC V2.

Signals are assumed to be generated only after a candle closes. Entry begins at
the next bar open. If stop and target are both touched inside one OHLC bar, stop
is assumed to fill first. Costs are explicit estimates, not broker quotations.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Sequence

from .core import Bar, SetupCandidate


@dataclass(frozen=True)
class TradeOutcome:
    symbol: str
    side: str
    signal_time_ms: int
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    initial_risk: float
    gross_r: float
    estimated_cost_r: float
    net_r: float
    mfe_r: float
    mae_r: float
    bars_held: int
    exit_reason: str


def simulate_trade(
    candidate: SetupCandidate,
    future_bars: Sequence[Bar],
    *,
    max_holding_bars: int = 96,
    fee_bps_per_side: float = 4.0,
    slippage_bps_per_side: float = 2.0,
) -> TradeOutcome | None:
    """Simulate a single candidate from the next bar; never enter on signal bar."""
    if max_holding_bars < 1:
        raise ValueError("max_holding_bars must be positive")
    if min(fee_bps_per_side, slippage_bps_per_side) < 0:
        raise ValueError("fees and slippage cannot be negative")
    if not future_bars:
        return None
    previous_time = candidate.signal_time_ms
    for bar in future_bars:
        if bar.timestamp_ms <= previous_time:
            raise ValueError("future bars must be strictly later than the signal bar")
        if (not all(isfinite(x) and x > 0 for x in (bar.open, bar.high, bar.low, bar.close))
                or bar.high < max(bar.open, bar.close, bar.low)
                or bar.low > min(bar.open, bar.close, bar.high)):
            raise ValueError("future bars contain invalid OHLC data")
        previous_time = bar.timestamp_ms

    entry_bar = future_bars[0]
    entry = entry_bar.open
    long_side = candidate.side == "LONG"
    if candidate.side not in {"LONG", "SHORT"}:
        raise ValueError("candidate side must be LONG or SHORT")
    risk = entry - candidate.stop_loss if long_side else candidate.stop_loss - entry
    if risk <= 0:
        return None
    target = candidate.take_profit
    if (long_side and target <= entry) or (not long_side and target >= entry):
        return None

    mfe = 0.0
    mae = 0.0
    exit_price = None
    exit_time = None
    reason = "TIME"
    bars_held = 0

    for bar in future_bars[:max_holding_bars]:
        bars_held += 1
        favourable = (bar.high - entry) if long_side else (entry - bar.low)
        adverse = (entry - bar.low) if long_side else (bar.high - entry)
        mfe = max(mfe, favourable / risk)
        mae = max(mae, adverse / risk)
        stop_hit = bar.low <= candidate.stop_loss if long_side else bar.high >= candidate.stop_loss
        target_hit = bar.high >= target if long_side else bar.low <= target

        if bars_held == 1:
            stop_gapped = bar.open <= candidate.stop_loss if long_side else bar.open >= candidate.stop_loss
            target_gapped = bar.open >= target if long_side else bar.open <= target
            if stop_gapped:
                exit_price, reason = bar.open, "STOP_GAP"
            elif target_gapped:
                exit_price, reason = bar.open, "TARGET_GAP"
        if exit_price is None and stop_hit and target_hit:
            exit_price, reason = candidate.stop_loss, "STOP_AMBIGUOUS_BAR"
        elif exit_price is None and stop_hit:
            exit_price, reason = candidate.stop_loss, "STOP"
        elif exit_price is None and target_hit:
            exit_price, reason = target, "TARGET"
        if exit_price is not None:
            exit_time = bar.timestamp_ms
            break

    if exit_price is None:
        last = future_bars[min(len(future_bars), max_holding_bars) - 1]
        exit_price, exit_time = last.close, last.timestamp_ms

    gross = (exit_price - entry) / risk if long_side else (entry - exit_price) / risk
    # Approximate entry+exit friction against the traded notional.
    friction_bps = 2.0 * (fee_bps_per_side + slippage_bps_per_side)
    cost_r = (entry * friction_bps / 10_000.0) / risk
    return TradeOutcome(
        symbol=candidate.symbol, side=candidate.side,
        signal_time_ms=candidate.signal_time_ms, entry_time_ms=entry_bar.timestamp_ms,
        exit_time_ms=exit_time, entry_price=entry, exit_price=exit_price,
        initial_risk=risk, gross_r=gross, estimated_cost_r=cost_r,
        net_r=gross - cost_r, mfe_r=mfe, mae_r=mae,
        bars_held=bars_held, exit_reason=reason,
    )


def summarize_outcomes(outcomes: Sequence[TradeOutcome]) -> dict[str, float | int]:
    """Return transparent descriptive stats; do not infer statistical significance."""
    if not outcomes:
        return {"trades": 0, "win_rate_pct": 0.0, "expectancy_r": 0.0,
                "profit_factor": 0.0, "max_drawdown_r": 0.0}
    values = [item.net_r for item in outcomes]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values),
        "win_rate_pct": len(wins) / len(values) * 100.0,
        "expectancy_r": sum(values) / len(values),
        "profit_factor": gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0),
        "max_drawdown_r": drawdown,
    }
