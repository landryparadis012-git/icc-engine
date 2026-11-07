"""Reusable ICC setup hypothesis, rebuilt on normalized real-volume bars.

This module generates candidates only. It does not place orders and makes no
claim that the setup has a positive expectancy. Inputs must be closed candles.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import median
from typing import Sequence


@dataclass(frozen=True)
class Bar:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    quote_volume: float | None = None


@dataclass(frozen=True)
class SetupCandidate:
    symbol: str
    side: str
    signal_time_ms: int
    reference_price: float
    stop_loss: float
    take_profit: float
    risk_per_unit: float
    score: float
    features: dict[str, float | bool | str]


def _valid_bars(bars: Sequence[Bar], minimum: int) -> bool:
    if len(bars) < minimum:
        return False
    previous = -1
    for bar in bars:
        values = (bar.open, bar.high, bar.low, bar.close)
        if bar.timestamp_ms <= previous or not all(isfinite(x) and x > 0 for x in values):
            return False
        if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(bar.open, bar.close, bar.high):
            return False
        if bar.volume is not None and (not isfinite(bar.volume) or bar.volume < 0):
            return False
        previous = bar.timestamp_ms
    return True


def ema(values: Sequence[float], period: int) -> float:
    if period < 1 or len(values) < period:
        raise ValueError("not enough values for EMA")
    alpha = 2.0 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def atr(bars: Sequence[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        raise ValueError("not enough bars for ATR")
    true_ranges = []
    for i in range(1, len(bars)):
        current, previous = bars[i], bars[i - 1]
        true_ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return sum(true_ranges[-period:]) / period


def rsi(closes: Sequence[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        raise ValueError("not enough values for RSI")
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes[-period:]]
    losses = [max(-change, 0.0) for change in changes[-period:]]
    avg_gain, avg_loss = sum(gains) / period, sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def _trend(bars: Sequence[Bar]) -> tuple[str, float, float, float, float]:
    closes = [bar.close for bar in bars]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    current_rsi = rsi(closes)
    if closes[-1] > e20 > e50:
        direction = "LONG"
    elif closes[-1] < e20 < e50:
        direction = "SHORT"
    else:
        direction = "FLAT"
    return direction, e20, e50, current_rsi, atr(bars)


def analyze_closed_bars(
    symbol: str,
    bars_15m: Sequence[Bar],
    bars_1h: Sequence[Bar],
    bars_1d: Sequence[Bar],
    *,
    stop_buffer_atr: float = 0.20,
    target_r: float = 1.50,
) -> SetupCandidate | None:
    """Evaluate the legacy trend/sweep idea with real volume and MTF context.

    All three inputs must contain closed bars and be timestamp-aligned by the
    caller. The final 15m bar is the signal bar; execution must occur no earlier
    than the next bar. Missing volume is represented as unavailable, never as a
    fabricated neutral value.
    """
    if stop_buffer_atr < 0 or target_r <= 0:
        raise ValueError("stop buffer must be non-negative and target_r positive")
    if not symbol or not _valid_bars(bars_15m, 60) or not _valid_bars(bars_1h, 60) or not _valid_bars(bars_1d, 60):
        return None

    trend15, e20, e50, rsi15, volatility = _trend(bars_15m)
    trend1h, _, _, rsi1h, _ = _trend(bars_1h)
    trend1d, _, _, rsi1d, _ = _trend(bars_1d)
    if trend15 == "FLAT" or volatility <= 0:
        return None

    current = bars_15m[-1]
    history = bars_15m[-33:-3]
    swing_low = min(bar.low for bar in history)
    swing_high = max(bar.high for bar in history)
    candle_range = max(current.high - current.low, 1e-12)
    body_fraction = abs(current.close - current.open) / candle_range
    bullish_sweep = current.low < swing_low and current.close > swing_low
    bearish_sweep = current.high > swing_high and current.close < swing_high

    if trend15 == "LONG":
        if not (rsi15 > 50 and (bullish_sweep or current.close > e20)
                and current.close > current.open and body_fraction >= 0.35):
            return None
        side = "LONG"
        stop = swing_low - volatility * stop_buffer_atr
        mtf = int(trend1h == "LONG") + int(trend1d == "LONG")
        rsi_alignment = int(rsi1h > 50) + int(rsi1d > 50)
        close_location = (current.close - current.low) / candle_range
        swept = bullish_sweep
    else:
        if not (rsi15 < 50 and (bearish_sweep or current.close < e20)
                and current.close < current.open and body_fraction >= 0.35):
            return None
        side = "SHORT"
        stop = swing_high + volatility * stop_buffer_atr
        mtf = int(trend1h == "SHORT") + int(trend1d == "SHORT")
        rsi_alignment = int(rsi1h < 50) + int(rsi1d < 50)
        close_location = (current.high - current.close) / candle_range
        swept = bearish_sweep

    risk = current.close - stop if side == "LONG" else stop - current.close
    if not isfinite(risk) or risk <= 0:
        return None
    target = current.close + risk * target_r if side == "LONG" else current.close - risk * target_r

    # Volume is only scored when source volume is actually present and usable.
    prior_volumes = [b.volume for b in bars_15m[-21:-1] if b.volume is not None and b.volume > 0]
    volume_ratio = None
    if current.volume is not None and current.volume > 0 and len(prior_volumes) >= 10:
        baseline = median(prior_volumes)
        if baseline > 0:
            volume_ratio = current.volume / baseline

    score = (
        25.0 * (trend15 != "FLAT")
        + 15.0 * (mtf / 2.0)
        + 10.0 * (rsi_alignment / 2.0)
        + 15.0 * min(1.0, max(0.0, (rsi15 - 50) / 20 if side == "LONG" else (50 - rsi15) / 20))
        + 10.0 * min(1.0, body_fraction)
        + 10.0 * close_location
        + 10.0 * float(swept)
    )
    if volume_ratio is not None:
        score += 5.0 * min(1.0, max(0.0, (volume_ratio - 0.8) / 1.2))

    return SetupCandidate(
        symbol=symbol.upper(),
        side=side,
        signal_time_ms=current.timestamp_ms,
        reference_price=current.close,
        stop_loss=stop,
        take_profit=target,
        risk_per_unit=risk,
        score=min(100.0, score),
        features={
            "ema20": e20,
            "ema50": e50,
            "rsi15": rsi15,
            "rsi1h": rsi1h,
            "rsi1d": rsi1d,
            "atr15": volatility,
            "body_fraction": body_fraction,
            "sweep": swept,
            "mtf_confirmations": float(mtf),
            "volume_ratio": volume_ratio if volume_ratio is not None else "unavailable",
        },
    )
