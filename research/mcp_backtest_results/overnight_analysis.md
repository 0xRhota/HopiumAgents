# Overnight Backtest Analysis — Apr 8, 2026

## Dataset
- **734 unique trades** across Nado (388) and Hibachi (346)
- Date range: Feb 9 - Apr 6, 2026
- Baseline: 40.9% WR, +$2,098.34 total PnL

## Critical Findings

### 1. BTC is killing us (-$624.63)
192 trades on BTC with 48.4% WR sounds decent, but the losses are bigger than the wins. BTC has the highest trade count and the worst PnL. Combined with DOGE (-$101), SOL (-$6), SUI (-$15), TAO (-$114) — removing these 5 symbols would have added **+$860 to PnL**.

**Recommendation**: Ban BTC from momentum bots or restrict to SHORT only. BTC moves too much for our 5% emergency SL — when it drops, it drops big.

### 2. LONGs massively outperform SHORTs
- LONG: +$3,078 (41.5% WR)
- SHORT: -$980 (40.4% WR)
- Nado LONG: +$3,069 vs Nado SHORT: -$947

**Recommendation**: Consider LONG-only mode on Nado, or require higher score threshold for SHORT entries.

### 3. Score thresholds are backwards
- Score 2.0-2.5: **60.8% WR**, +$110 (best bucket!)
- Score 3.0-3.5: 32.9% WR, -$126 (current minimum threshold)
- Score 4.5-5.0: 40.1% WR, -$218

Our v9 scoring system's higher scores correlate with WORSE performance. This could mean:
- Extreme scores = chasing extended moves that reverse
- Moderate scores = catching early momentum with room to run

**Recommendation**: Lower score_min back to 2.5, OR add a score_max of 4.0 to filter out overextended signals.

### 4. Quick exits win big, long holds break even
- 0-5min holds: +$1,655 (33% WR but avg win is massive)
- 15-60min: +$295 (47% WR, best balance)
- 1-4hr: -$25 (break even)
- 24hr+: +$5 (72% WR but tiny PnL)

**Recommendation**: Keep TREND_FLIP exits but add a time-based partial close at 4hr mark for positions that haven't flipped.

### 5. TP exits are 97% WR, SL exits are 8.6% WR
- TP: 165 trades, $+2,250 (the money maker)
- SL: 268 trades, $-95 (lots of small losses — acceptable)
- EMERGENCY_SL: 12 trades, $-357 (catastrophic — avg -$30 per trade)

The old TP/SL system actually worked well. The switch to TREND_FLIP-only exits (no fixed TP/SL) removed TP exits which were our best performers.

**Recommendation**: Re-add fixed TP at 1.5% alongside TREND_FLIP. The data strongly supports it.

### 6. Star symbols: LIT, ETH, kBONK, UNI, XMR
- LIT: 40 trades, 57.5% WR, +$1,142
- ETH: 71 trades, 28.2% WR, +$383 (low WR but big winners)
- kBONK: 25 trades, 32% WR, +$312
- UNI: 8 trades, 75% WR, +$210
- XMR: 21 trades, 42.9% WR, +$193

### 7. Best composite filter
**Remove worst 5 symbols (BTC, DOGE, SOL, SUI, TAO)**:
- 363 trades (vs 734 baseline)
- 40.2% WR
- +$2,958.87 PnL (+$860 improvement)
- Cuts volume in half but dramatically improves PnL

## Funding Rate Analysis
Binance Futures API is geo-blocked (451). OKX works for current rates but historical funding at trade timestamps requires a different approach.

**Recommendation**: Install funding-rates-mcp for live monitoring. Test funding rate alignment as a live filter going forward rather than backtesting.

## MCP Tools Assessment

### Worth Installing (free, proven value)
1. **funding-rates-mcp** — Funding rates across exchanges. Our CLAUDE.md already defines funding zones but we never wired them in. Data supports using it as entry filter.
2. **crypto-indicators-mcp** — 50+ indicators via CCXT. Useful for cross-validating our v9 signals with Bollinger, ATR, VWAP.

### Not Worth It (premium, 404'd or irrelevant)
- **Cerebrus Pulse** — repo is 404. Can't evaluate.
- **SignalFuse** — Hyperliquid-specific, we don't trade there.
- **Apollo Intelligence** — repo is 404.
- **Coinversaa Pulse** — Hyperliquid only.

### Installed But Unused
- **funding-rates-mcp** — cloned to `tools/funding-rates-mcp`, deps installed
- **crypto-indicators-mcp** — cloned to `tools/crypto-indicators-mcp`, deps installed

## Action Items (prioritized)
1. **HIGH**: Remove or restrict BTC from momentum bots (saves $624)
2. **HIGH**: Consider LONG-only on Nado (saves $947 from bad shorts)
3. **MEDIUM**: Re-add fixed TP at 1.5% alongside TREND_FLIP exits
4. **MEDIUM**: Test score_min 2.5 with score_max 4.0
5. **LOW**: Wire in funding-rates-mcp as live entry filter
6. **LOW**: Add 4hr partial close for stale positions
