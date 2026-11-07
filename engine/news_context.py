"""Validated GitHub repository_dispatch context for post-release ICC scans.

News context is annotation only: it never creates or overrides a trading signal.
"""
import json
import time

MAX_EVENT_AGE_S = 45 * 60
FUTURE_SKEW_S = 120

SYMBOL_CURRENCIES = {
    "XAUUSD": {"USD"},
    "GBPUSD": {"GBP", "USD"},
    "US30": {"USD"},
    "NAS100": {"USD"},
    "USOIL": {"USD"},
    "BTC": {"USD"},
}


def parse_github_event(event, now=None):
    """Return a fresh post-news payload or a valid pre-release guard."""
    if not isinstance(event, dict):
        return None
    action = event.get("action") or event.get("event_type")
    payload = event.get("client_payload")
    if not isinstance(payload, dict):
        return None
    now = int(time.time() if now is None else now)
    try:
        event_ts = int(payload.get("event_ts"))
    except (TypeError, ValueError):
        return None
    currency = str(payload.get("currency") or "").strip().upper()
    title = str(payload.get("event") or "").strip()
    if not currency or not title:
        return None

    if action == "icc.news_guard" and payload.get("reason") == "pre-news guard":
        # Guard dispatch is intentionally sent up to 45m before release.
        if event_ts < now - FUTURE_SKEW_S or event_ts > now + MAX_EVENT_AGE_S:
            return None
        return {
            "kind": "guard", "currency": currency, "event": title[:160],
            "event_ts": event_ts, "block_until": event_ts + 90,
            "key": f"{event_ts}:{title[:40]}",
        }

    if action != "icc.run" or payload.get("reason") != "post-news scan":
        return None
    age = now - event_ts
    if age < -FUTURE_SKEW_S or age > MAX_EVENT_AGE_S:
        return None
    return {
        "kind": "post", "currency": currency, "event": title[:160],
        "event_ts": event_ts,
        "actual": str(payload.get("actual") or "").strip()[:80],
        "forecast": str(payload.get("forecast") or "").strip()[:80],
        "previous": str(payload.get("previous") or "").strip()[:80],
    }

def load_github_event(path):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return parse_github_event(json.load(f))
    except (OSError, ValueError, TypeError):
        return None


def is_relevant(context, symbol):
    """Whether a scanned instrument is materially related to the event currency."""
    if not isinstance(context, dict):
        return False
    currency = str(context.get("currency") or "").upper()
    return currency in SYMBOL_CURRENCIES.get(symbol, set())


def format_context(context, symbol):
    """Annotate only symbols materially related to the event currency."""
    if not is_relevant(context, symbol):
        return ""
    currency = str(context.get("currency") or "").upper()
    parts = [f"📰 POST-NEWS CONTEXT (not a direction signal): {currency} · {context.get('event', 'release')}"]
    if context.get("actual"):
        parts.append(f"actual {context['actual']}")
    if context.get("forecast"):
        parts.append(f"forecast {context['forecast']}")
    if context.get("previous"):
        parts.append(f"previous {context['previous']}")
    return "\n" + " | ".join(parts)
