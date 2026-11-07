# ICC V2 — Multi-Asset Intelligence Engine

ICC V2 is being developed alongside the existing ICC engine, not as a replacement commit. The current engine remains untouched while V2 establishes a new data contract, provider adapters, feature pipeline, strategy evaluation, historical memory, and demo execution boundary.

## Initial architecture

```text
Market venues / licensed providers
        |
Provider adapters (venue-specific)
        |
Normalized market-data contracts
        |
Quality checks + timestamp alignment
        |
Feature and market-flow calculations
        |
ICC setup hypotheses + historical validation
        |
Opportunity ranking + portfolio-level constraints
        |
Demo execution adapter
        |
Immutable event ledger + performance analysis
```

## First adapter: Binance public Spot REST

`icc_v2.data.binance_public.BinancePublicSpot` is the first integration spike. It uses public market endpoints only and does not accept API keys or submit orders. It exposes:

- exchange instrument metadata and trading status;
- 24-hour ticker statistics;
- OHLCV candles with actual exchange-reported base-asset volume and quote-asset volume;
- recent public trades;
- current order-book snapshots.

This first adapter is intentionally a **REST snapshot adapter**. It is not yet a streaming order-flow engine. A current depth snapshot cannot reconstruct historical depth changes, and REST polling must not be presented as continuous order flow. A later WebSocket adapter must maintain sequence-aware local books and capture trade events without gaps.

## Data semantics

- Every record carries venue, market type, instrument, event time and receive time where applicable.
- Candle `volume` is base-asset volume; `quote_volume` is quote-asset volume.
- Public trades preserve exchange trade ID, price, quantity, quote quantity, timestamp and buyer-is-maker flag. Aggressor-side interpretation must be documented and tested before use.
- Order-book snapshots are point-in-time bid/ask levels, not a consolidated market book.
- No synthetic volume, candle-range weighting or inferred global forex order book is allowed to masquerade as exchange-observed flow.
- Provider failures, rate limits, malformed payloads and stale data must fail visibly; no silent substitution.

## Scope and safety

- Initial adapter: Binance Spot public data only.
- No private account access, order placement, leverage changes or live trading.
- No broker/prop-firm credentials in this package.
- Future execution begins against an explicitly configured demo environment, with platform rules, a kill switch and a complete order/fill ledger.
- Crypto, FX, futures, equities and CFDs require separate market metadata and source-quality policies. They must not be treated as interchangeable.

## Run the adapter

From the repository root:

```bash
PYTHONPATH=v2 python -m unittest discover -s v2/tests -v
```

The adapter uses Python's standard library and has no mandatory third-party dependency.

## Planned next steps

1. Add unit tests for response parsing, retries, rate-limit handling and invalid symbols.
2. Add the WebSocket transport loop with reconnect, ping/pong, trade deduplication and automatic resnapshot on depth sequence gaps. The pure depth-book reconstruction primitive is now in place.
3. Add a normalized event store and data-quality monitoring.
4. Add instrument discovery across multiple exchanges, with per-venue capability metadata.
5. Build historical setup evaluation before enabling any demo execution.
6. Connect the demo execution adapter only after the selected prop firm's trial API and rules are verified.

## Fincept relationship

Fincept is an integration reference and possible connector host, not a required runtime dependency. ICC V2 owns its normalized contracts and can consume Fincept-provided data where the source, timestamp, licence and data semantics are known. Do not copy Fincept source code into this repository without resolving its licensing terms.


## Added V2 strategy and futures work

The current V2 branch now also includes:
- A public Binance USD-M perpetual adapter for active perpetual discovery, klines, aggregate trades and depth snapshots. Klines preserve exchange-reported base volume, quote volume, trade count and taker-buy volume fields.
- A rebuilt trend/sweep setup hypothesis using normalized closed bars, 15m signal conditions and 1h/daily confirmation. Actual relative volume is scored only when present; missing volume is never replaced with a fabricated neutral value.
- Conservative OHLC backtest primitives: next-bar-open entry, stop-first treatment when stop and target are both touched in one candle, explicit fee/slippage assumptions, MFE/MAE and expectancy/drawdown summaries.

These are research components only. A full FX adapter, live WebSocket transport, cross-asset universe scanner, durable V2 memory and walk-forward portfolio evaluation remain outstanding. FX spot has no single consolidated exchange order book; any broker tick volume or futures volume must be identified by source and market type.

The V2 test suite runs with:

```bash
PYTHONPATH=v2 python -m unittest discover -s v2/tests -v
```


## Binance Spot live stream transport

`icc_v2.data.binance_stream.BinanceSpotWebSocket` adds a read-only combined
trade and diff-depth stream consumer. Install the optional runtime dependency
with `pip install websocket-client`. The stream uses public endpoints only.

- Subscribes to trade and 100 ms diff-depth streams for an explicit symbol list.
- Deduplicates public trades by exchange trade ID and preserves the buyer-is-maker
  flag plus the corresponding aggressor-side interpretation.
- Buffers depth events and bridges them to a REST snapshot using the existing
  sequence-aware book reconstruction primitive.
- Emits depth only after the local book is synchronized. Sequence gaps invalidate
  the book and propagate an error so the caller reconnects and resnapshots.
- Bounds the bootstrap buffer and uses bounded exponential reconnect backoff.

This is an initial transport component, not yet a continuously running service,
persistent event store, or multi-venue feed manager. A socket session ending
invalidates its local books; consumers must treat the next session as a fresh
bootstrap. The event callback must persist or forward events if retention is
required. WebSocket transport tests use injected sockets and REST snapshots.
