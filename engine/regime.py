"""regime — VIX-based market regime detection (FRED VIXCLS, keyless).

Ported idea from OpenTerminal's FRED provider: pull the daily VIX close and
classify the volatility environment. The engine blends this with its existing
EMA trend regime (s60 bull/bear) to tag candidates:

  label    ∈ {"calm", "normal", "stress", "panic"}
  risk_off → True when VIX says trade smaller / avoid counter-trend rescues

Thresholds (classic VIX bands):
  < 14  calm   | 14–19 normal | 19–25 stress | > 25 panic

FRED's CSV is daily and lags the cash close by a few hours — fine for a
regime tag. Never raises: on any failure returns a neutral "unknown" regime
so the engine keeps scanning.
"""

import data_layer


def get_vix():
    """Latest VIX close: Yahoo ^VIX first (fast), FRED VIXCLS as fallback.
    Returns float or None on failure — regime must never break a scan."""
    try:
        bars = data_layer.fetch_candles("^VIX", "1d", "1mo", tries=1)
        if bars:
            return bars[-1][4]
    except Exception as e:  # noqa: BLE001
        print(f"[regime] Yahoo ^VIX failed: {e}")
    try:
        return data_layer.fred_vix()
    except Exception as e:  # noqa: BLE001
        print(f"[regime] FRED VIX failed: {e}")
        return None


def classify(vix):
    if vix is None:
        return "unknown", False
    if vix < 14:
        return "calm", False
    if vix < 19:
        return "normal", False
    if vix < 25:
        return "stress", True
    return "panic", True


def current():
    """→ (label, risk_off, vix_value). Cached per-process run."""
    global _CACHED
    if _CACHED is None:
        v = get_vix()
        label, risk_off = classify(v)
        _CACHED = (label, risk_off, v)
        print(f"[regime] VIX={v} → {label} risk_off={risk_off}")
    return _CACHED


_CACHED = None
