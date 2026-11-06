# ICC Order-Flow Engine V2 — Shadow/Paper Design

Status: design-only. The current production strategy remains unchanged. V2 must not place broker orders or send live trade-entry instructions.

## Decision
Build a separate, versioned V2 research path for both gold and crypto. Preserve the existing engine as the baseline and run V2 in shadow/paper mode until independent evidence supports a review.

## Why a rebuild is justified
The current data layer reduces market data to OHLC tuples and discards real volume. Some downstream features use placeholder or price-derived volume proxies. Those inputs cannot support genuine order-flow claims. V2 must not reinterpret these proxies as executed buy/sell volume.

## Market/data split
### Crypto
Start with one liquid, exchange-specific instrument (for example BTC/USDT) where the source provides timestamped trades and incremental order-book updates. Record venue, symbol, sequence number, event timestamp, receive timestamp, bid/ask levels, and trade aggressor side when the venue provides it.

### Gold
Do not treat a CFD candle feed or Yahoo `GC=F` price series as exchange order flow. Preferred research feed is COMEX GC futures trades and depth from a licensed source. Preserve contract month, tick size, venue, session, timestamps, and roll metadata. If only XAUUSD spot/CFD candles are available, label the feed as quote/tick proxy and disable true CVD/footprint/queue-imbalance features.

Fincept Terminal can be evaluated as a local data-source adapter/visualization, but its local MCP server is not a cloud endpoint. A separate collector must run where the terminal/feed is available and push normalized, signed, read-only observations to the cloud. Review the Fincept AGPL-3.0-or-later obligations before modifying, redistributing, or exposing a modified terminal as a network service.

## V2 architecture
1. `adapters/`: independent crypto venue and COMEX gold adapters; normalize source timestamps and sequence IDs.
2. `storage/`: append-only raw trade/depth snapshots or compact event batches; never overwrite source data.
3. `features/`: trade delta, cumulative volume delta, book imbalance, order-flow imbalance, absorption, replenishment, liquidity pulls, sweeps, footprint clusters, session VWAP and volume profile.
4. `signals/`: explicit, versioned setup hypotheses with entry/invalidity/stop/target context.
5. `replay/`: deterministic event-time replay and walk-forward evaluation.
6. `shadow/`: compare V2 candidate signals with current ICC decisions and log every candidate, veto, and missed setup with reason.
7. `reports/`: daily data-quality, coverage, candidate, and outcome reports. No execution module in V2.

## Required data contract
Every event must include:
- `venue`, `instrument`, `market_type`, `event_type`
- `event_ts`, `received_ts`, `sequence` (when supplied)
- trade: `price`, `quantity`, `aggressor_side` (unknown remains unknown)
- book: ordered bid/ask levels with price and size
- `source`, `contract` / `expiry` where applicable
- `quality_flags` and `is_complete`

Reject or quarantine out-of-order, stale, incomplete, crossed, malformed, or sequence-gapped book data. Never fill missing aggressor side with an inferred direction unless clearly marked as an estimate and excluded from primary metrics.

## Strategy research sequence
1. Instrument data coverage and quality before strategy design.
2. Baseline replay current ICC strategy and preserve its exact decisions/outcomes.
3. Build feature tests on synthetic known order-flow cases.
4. Define separate gold and crypto setups; do not assume one threshold transfers across markets.
5. Use time-based train/validation/test splits and walk-forward evaluation.
6. Include spread, fees, slippage, latency, contract roll, funding, and session effects where relevant.
7. Report expectancy, drawdown, adverse/favourable excursion, hit rate, trade count, and confidence intervals; compare against baseline and simple controls.
8. Run shadow signals alongside current ICC for a pre-agreed sample and calendar period.

## Safety invariants
- V2 has no broker API keys, order endpoints, or live execution permissions.
- V2 cannot modify current strategy thresholds, memory, position state, or trade ledger.
- Existing risk firewall remains independent and veto-only.
- Current engine stays the only production strategy until a separate human-approved migration PR.
- No model is promoted based on in-sample win rate alone.
- Missing/poor feed quality suppresses V2 signals; it never falls back to fabricated volume.

## Initial deliverables
- Read-only market data schema and adapters
- Data quality monitor for both markets
- Candidate/rejection ledger and missed-trade replay
- Deterministic replay harness with synthetic tests
- Shadow signal reports with source provenance

## Explicit non-goals
No live execution, no modification of the existing strategy, no auto-promotion, and no change to current production cron or credentials.
