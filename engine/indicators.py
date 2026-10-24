"""indicators — port of OpenTerminal web/lib/indicators.ts to Python.

All functions take/return plain lists so the engine can use them directly:
  candles: [(ts, open, high, low, close), ...]  newest last
  closes:  [float, ...]
Returns scalar helpers for the engine's scoring, or full series where the
caller wants them.
"""

import math


def sma(closes, period):
    """Simple moving average of the last `period` closes (scalar)."""
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    return sum(closes[-period:]) / period


def sma_series(closes, period):
    out = []
    for i in range(period - 1, len(closes)):
        out.append(sum(closes[i - period + 1:i + 1]) / period)
    return out


def ema_series(closes, period):
    """Full EMA series (same recursion as TS ema())."""
    if not closes:
        return []
    k = 2 / (period + 1)
    prev = None
    out = []
    for c in closes:
        prev = c if prev is None else c * k + prev * (1 - k)
        out.append(prev)
    return out


def ema(closes, period):
    """Scalar: last EMA value."""
    s = ema_series(closes, period)
    return s[-1] if s else 0.0


def vwap(candles):
    """Cumulative VWAP over the whole candle window (scalar, last value)."""
    cum_pv = 0.0
    cum_v = 0.0
    for ts, o, h, l, c in candles:
        typical = (h + l + c) / 3
        # engine tuples carry no volume → weight by bar range as proxy
        vol = max(h - l, 1e-9)
        cum_pv += typical * vol
        cum_v += vol
    return (cum_pv / cum_v) if cum_v > 0 else (candles[-1][4] if candles else 0.0)


def rsi(closes, period=14):
    """Wilder-smoothed RSI (scalar, last value). 50.0 when not enough data."""
    if len(closes) < period + 1:
        return 50.0
    avg_gain = 0.0
    avg_loss = 0.0
    n = len(closes)
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        if i <= period:
            avg_gain += gain / period
            avg_loss += loss / period
            if i < period:
                continue
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        last = 100 - 100 / (1 + rs)
    return last if avg_loss != 0 else 100.0


def macd(closes, fast=12, slow=26, signal=9):
    """Returns (macd_line_last, signal_last, histogram_last) scalars."""
    if len(closes) < slow + signal:
        return 0.0, 0.0, 0.0
    fast_e = ema_series(closes, fast)
    slow_e = ema_series(closes, slow)
    align = len(fast_e) - len(slow_e)
    macd_line = [fast_e[align + i] - slow_e[i] for i in range(len(slow_e))]
    k = 2 / (signal + 1)
    prev = None
    sig = []
    for v in macd_line:
        prev = v if prev is None else v * k + prev * (1 - k)
        sig.append(prev)
    if not sig:
        return 0.0, 0.0, 0.0
    return macd_line[-1], sig[-1], macd_line[-1] - sig[-1]


def bollinger(closes, period=20, mult=2):
    """Returns (upper, middle, lower) scalars for the latest bar."""
    if len(closes) < period:
        c = closes[-1] if closes else 0.0
        return c, c, c
    window = closes[-period:]
    mean = sum(window) / period
    var = sum((c - mean) ** 2 for c in window) / period
    sd = math.sqrt(var)
    return mean + mult * sd, mean, mean - mult * sd
