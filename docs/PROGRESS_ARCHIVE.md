## History (2026-04-16)

### Active Bots (running since Apr 13 restart, ~80h uptime, no crashes)

| # | Process | PID | Config | Log File |
|---|---------|-----|--------|----------|
| 1 | Hibachi Bot | 33479 | 6 assets, 3×$50, TP=80bps SL=40bps, score>=2.5 | `hibachi_bot.log` |
| 2 | Nado Bot | 33636 | 21 assets, 2×$100, TP=80bps SL=40bps, score>=2.5 | `nado_bot.log` |
| 3 | Paradex Bot | 26051 | BTC/ETH/SOL swing, **qwen-max** (Qwen 3 235B), 300s | `paradex_live_v2.log` |
| 4 | Monitor | 16629 | 5min cadence across exchanges | `monitor.log` |

### Exchange Equity (source of truth — always query exchange APIs)

| Exchange | Starting (Apr 13 12:48) | Current (Apr 16 09:48) | Δ |
|----------|-------------------------|------------------------|---|
| Hibachi | $28.97 | $23.81 | **−$5.16** |
| Nado | $54.91 | $53.37 | **−$1.54** (+ $107 LIT LONG open) |
| Paradex | $27.65 | $27.67 | **+$0.02** (1 BTC LONG held all week) |
| Extended | $0.54 | $0.42 | **−$0.12** (bot off, stale JUP+OP positions) |
| **TOTAL** | **$112.07** | **$105.27** | **−$6.80** |

**Open positions**: Hibachi BNB SHORT $39.50 notional, Nado LIT LONG $107.40.

### PnL Tracking Is Broken — MUST FIX (Apr 16)

**Problem**: The per-trade `pnl` field in JSONL logs is `(exit - entry) × size` — **gross, excludes taker fees, funding, and open positions.** Over 69h the bot JSONL aggregations diverged wildly from exchange equity:

| | Bot gross PnL (JSONL) | Exchange Δ | Gap |
|--|--|--|--|
| Hibachi | −$3.22 | −$5.16 | $1.94 (fees on 73 trades) |
| Nado | +$6.09 | −$1.54 | $7.63 (fees on 142 trades + funding on LIT) |

**User directive (Apr 16)**: "dont show me bullshit pnl without fees. i only care about real data" and "you must accurately track fees and everything." **Bot PnL without fees is NEVER to be presented as truth.**

**HARD RULE**: Only exchange equity deltas are truth. Never lead with bot-internal gross PnL. This is saved in Claude memory.

**Fix plan (two layers, pending implementation)**:

Layer 1 — **Reporting** (immediate priority): Build `scripts/real_pnl.py` that queries each exchange's native PnL endpoint for a time window. Nado already has `nado_sdk.get_pnl(hours=N)` → returns realized_pnl + fees + net_pnl from Archive API. Hibachi and Paradex need equivalent wrappers. No bot restart needed (read-only).

Layer 2 — **Per-trade enrichment** (deeper fix): After every `close_position()`, query the fill/match record for that trade. Add `fee_in`, `fee_out`, `funding`, `pnl_net` fields to the JSONL record. Requires bot restart to pick up.

### Oct 2025 Lighter Exchange Hack — Investigation (Apr 15)

User requested investigation into the October 2025 incident where an ETH private key was exposed while integrating Lighter DEX, resulting in wallet drain of **$433.76** from `0xCe9784FcDaA99c64Eb88ef35b8F4A5EabDC129d7`.

**Recovered data**:
- `docs/SECURITY_AUDIT_REPORT.md` — 386-line formal audit from 2025-10-29
- `docs/OCT2025_CHAT_HISTORY_RECOVERED.md` — **NEW**: 119 prompts from Oct-Nov 2025 extracted from `~/.claude/history.jsonl`. Full conversation transcripts were deleted by user on Oct 10 2025 but prompt history survived.
- `archive/2025-11-03-cleanup/LIGHTER_RESEARCH_SUMMARY.md` — Evidence showing Lighter's docs at `https://apidocs.lighter.xyz/docs/get-started-for-programmers-1#/` directed users to provide ETH_PRIVATE_KEY for SDK setup, with no mention of web UI alternative. Lighter team acknowledged documentation gap.
- Root cause: user pasted ETH_PRIVATE_KEY in Claude Code chat during Lighter SDK debugging; only an API_KEY_PRIVATE_KEY was needed for trading.
- Session transcripts don't exist (93 Oct 2025 session UUIDs found in `~/.claude/todos/` but zero matching JSONL files — deliberately wiped Oct 10).

### Outstanding Priority Fixes

1. ~~Fix PnL logging (reconciled positions)~~ — **DONE Apr 13**
2. ~~Pause or fund Nado~~ — **DONE** (funded to $55)
3. **FIX: Accurate fee+funding tracking** — CRITICAL. Bot PnL is gross/pre-fee, diverges from exchange by $2-8 over 69h. Need: (a) `real_pnl.py` reporting script querying exchange APIs, (b) per-trade fee enrichment in JSONL. Nado `get_pnl()` already works; Hibachi/Paradex need wrappers.
4. **Map new Nado products** — 29 unmapped (14 equity perps). SDK `PRODUCT_SYMBOLS` in `dexes/nado/nado_sdk.py`.
5. **Add 1H EMA trend filter** — backtest showed +$0.37/day lift. Not in live bot.
6. **Build Paradex momentum adapter** — user wants Paradex on same v10 momentum strategy as Hibachi/Nado. Need `ParadexAdapter(ExchangeAdapter)` at `core/strategies/momentum/exchange_adapter.py`.

---

## History (2026-04-14)

### Bugs Fixed (Apr 13) — All three closed

**1. Paradex decision engine — total failure (`orchestrator/simplified_decision_engine.py`)**
- Symptom: every decision cycle failed with `ValueError: Unknown model 'qwen/qwen3.6-plus:free'`.
- Root cause: `_normalize_model_name()` alias map returned OpenRouter **model_id strings** (`qwen/qwen3.6-plus:free`), but those were then passed as `ModelClient` **config keys** — which only accepts `qwen-max`, `qwen-free`, `gpt-5.1-instant`, etc.
- Fix: rewrote alias map to return valid `ModelClient` keys. Default model changed from `qwen/qwen3.6-plus:free` → `qwen-max`. Bot now receives Qwen BUY/SELL/NO_TRADE JSON decisions at ~$0.0001/call.
- **NOTE**: User speculated "maybe we switched to gemma" — no gemma references anywhere in repo. We're on Qwen 3 235B (qwen-max).

**2. PnL logging — fake +$104 bug (`scripts/momentum_mm.py`)**
- Symptom: PROGRESS.md Apr 10 noted "Bot internally showed Nado +$104 when exchange said -$6."
- Root cause: **ONE reconciled ETH position** on Apr 9 with `entry_price=$0.00` (adopted from exchange at startup). Exit calc `(2206.10 - 0) × 0.048 = +$105.89` produced a fake profit that dominated all other symbol PnL. All other symbols tracked correctly.
- Fix: detect `entry_price<=0` at close time → set `pnl=None`, set `reconciled: true` flag in JSONL, skip self-learning feed (was poisoning score bucket WR stats), log `PnL=UNKNOWN (reconciled, entry_price=0)` in the trade log line.

**3. Watchdog silent since schema rename (`scripts/watchdog.py`)**
- Symptom: daily PnL summaries always showed $0.
- Root cause: reading legacy `pnl_delta` field, but current schema writes `pnl` and `pnl_balance_delta`. Schema was renamed long ago, watchdog never updated.
- Fix: read `pnl` with `pnl_delta` legacy fallback, exclude reconciled trades from totals, show `[RECON]` flag on per-trade lines.

---

## Current Status (2026-04-10)

### Active Bots — v10 Backtest-Optimized Strategy

| # | Process | PID | Config | Log File |
|---|---------|-----|--------|----------|
| 1 | Hibachi Bot | 34521 | 6 assets, 3×$50, TP=80bps SL=40bps, score>=2.5 | `hibachi_bot.log` |
| 2 | Nado Bot | 34117 | 20 assets, 2×$100, TP=80bps SL=40bps, score>=2.5 | `nado_bot.log` |
| 3 | Paradex Bot | 16420 | BTC swing, Qwen 3.6 decisions | `paradex_live_v2.log` |

**Balances (Apr 10)**: Hibachi $29, Nado $20, Paradex active

### v10 Strategy Deployment (Apr 9)

Deployed backtest-optimized strategy to both exchanges. Key changes from v9:
- **Re-added fixed TP/SL**: TP=80bps (0.8%), SL=40bps (0.4%), max_hold=2hr
- **Lowered score threshold**: 3.0 → 2.5 (more entries, faster capital recycling)
- **Disabled volume gate**: `require_volume=False` (more signals)
- **Exchange-specific sizing**:
  - Nado: 2 positions × $100 notional (40% margin on 10x)
  - Hibachi: 3 positions × $50 notional (60% margin on 5x)
- Exit priority: TP → SL → TIME → TREND_FLIP → EMERGENCY_SL

**First hour results (Apr 9)**: 3 TP wins, 0 losses, +$3.31 combined equity. Strategy looked strong.

### v10 Overnight Results (Apr 9-10) — Mixed

**Hibachi: WORKING** ✓
- 22 trades, 45.5% WR, **+$0.51 PnL**, max drawdown $3.35
- Avg win: $0.29, avg loss: $0.20, R:R 1.45
- Exactly matching backtest predictions. Stable, small positive.

**Nado: BLEEDING** ✗
- 34 trades, 35.3% WR, **-$5 equity loss** overnight
- Exchange 24h: 81 trades, -$4.22 realized, -$1.88 fees, net **-$6.10**
- Problem: $100 min notional on $25 account = 2% risk per SL hit. 18 SL hits = -$12. Account too small.
- **Need $70+ equity on Nado** for same risk profile as Hibachi (0.7% per SL)

**Root cause**: Hibachi can size trades to $50 (0.7% risk per SL). Nado forced to $100 minimum (2% risk per SL). Same strategy, different risk profiles due to exchange minimums.

### PnL Logging Bug (STILL BROKEN)

Bot internally showed Nado +$104 when exchange said -$6. Same old bug: TREND_FLIP exits record `equity_after - equity_before` (balance delta) which gets polluted by unrealized PnL from other positions. **Must fix — cannot trust bot PnL numbers for Nado.**

Hibachi PnL tracking is closer to reality (+$0.51 internal vs -$0.36 exchange) but still not exact.

### Nado Equity Perps (NEW — Apr 10)

Nado added **14 equity perpetuals** + ~15 new crypto tokens (product IDs 58-114). SDK needs mapping update.

**Equity perps identified by oracle price matching:**
| ID | Symbol | Price |
|----|--------|-------|
| 96 | AAPL | $159 |
| 112 | AMZN | $189 |
| 76 | AMD | $55 |
| 110 | AVGO | $372 |
| 90 | COIN | $99 |
| 104 | GOOGL | $239 |
| 102 | META | $261 |
| 68 | MSFT | $445 |
| 106 | MSTR | $319 |
| 108 | NFLX | $632 |
| 100 | NVDA | $681 |
| 88 | PLTR | $76 |
| 114 | QQQ | $347 |
| 98 | TSLA | $612 |

**Not yet mapped in SDK** (`dexes/nado/nado_sdk.py` PRODUCT_SYMBOLS dict needs update).

### Priority Fixes

1. **FIX PnL logging** — Use calculated (entry-exit) PnL, not balance delta. This bug has persisted across 50+ sessions.
2. **Pause or fund Nado** — $20 equity is too small for $100 min. Either add $50+ or pause until funded.
3. **Map new Nado products** — 29 unmapped products including equity perps.
4. **Add 1H EMA trend filter** — Backtest showed +$0.37/day with filter vs -$0.35 without. Not yet in live bot code.

### Qwen 3.6 Upgrade (Apr 6)

All Qwen model references updated to **`qwen/qwen3.6-plus:free`** (free on OpenRouter):
- `scripts/momentum_mm.py`, `scripts/grid_mm_nado_v8.py`, `orchestrator/simplified_decision_engine.py`
- LLM filter still disabled on momentum bots. Paradex using Qwen 3.6.

### Strategy Backtest (Apr 8-9)

**Full backtest**: 13 strategies tested over 30 days on 16 symbols using Binance 15m candles.
- Reports: `research/mcp_backtest_results/strategy_backtest_v2_results.json`, `realistic_backtest_results.json`
- Qwen 3.5 analysis: `research/mcp_backtest_results/qwen_strategy_recommendations.md`

**Corrected baseline** (690 clean trades): -$74 PnL, 35.8% WR. Strategy has no meaningful edge on any single symbol — the variations are noise on small samples.

**Key finding**: The strategy isn't broken, it's a **sizing problem**. Hibachi can size to $50 (works). Nado can't go below $100 (bleeds on small accounts).

**Profitable strategies (realistic $50 sizing)**:
| Strategy | Exchange | Trades/day | Vol/day | PnL/day |
|----------|----------|-----------|---------|---------|
| Short Bias + 1H Filter | Nado (2×$100) | 35.0 | $7,009 | +$0.37 |
| Scalp (3×$50) | Hibachi | 10.9 | $1,087 | +$0.16 |
| Balanced (3×$50) | Hibachi | 10.6 | $1,057 | +$0.16 |

**MCP tools installed** (not wired in): `tools/funding-rates-mcp`, `tools/crypto-indicators-mcp`

### Nado Account Nuked (Mar 23 → Apr 6)

**What happened**: XMR and LIT positions had **frozen indicator scores** (XMR=4.3, LIT=5.0 every cycle for weeks). TREND_FLIP exit never triggered. Positions bled until liquidation. Equity went $15 → $3.93 → $0.00.

**Root cause**: Stale Binance candle data for XMR and LIT — scores never changed, so exits never fired. Emergency SL (5%) checks position PnL, not account health, so it didn't catch the slow bleed either.

**Fix needed**: Investigate why XMR/LIT indicator data was frozen. TODO in future session.

### Nado Signer Delinked AGAIN (Mar 22) — 3rd time

**Problem**: Signer broke again between Mar 13-22. Same error 2028, same mismatch (`0x36e82b22...` replacing `0xd086A7a8...`). Nado hadn't traded since Mar 13 — 1253 entry attempts, 2437 order failures.

**Root cause**: Logging into Nado UI and toggling 1-Click Trading replaces our linked signer with Nado's auto-generated one. User may have unknowingly toggled it when logging in.

**Fix**: Re-linked via MetaMask (nonce 10). Bot restarted as PID 74080.

**⚠️ RECURRING ISSUE**: This has happened 3 times (Mar 2, Mar 13, Mar 22). Added prominent warning to SCRATCHPAD.md: **DO NOT toggle 1-Click Trading in Nado UI.**

**Nado self-learning also blocking**: 4.0-4.5 bucket at 12% WR (8 trades) — below 25% threshold. 3.5-4.0 at 22% WR (9 trades). Some entries getting through but many blocked.

### Critical Fixes (Mar 2-13)

**Nado Signer Key Mismatch (Mar 2-8)** — Bot couldn't place orders:
- **Symptom**: 468 entry attempts, 910 signature errors (error_code 2028), 0 fills since Mar 2
- **Root cause**: Nado linked signer on exchange (`0x36e82b22...`) didn't match the key in `.env` (`0xd086A7a8...`). The `.env` key was generated by our `generate_nado_linked_signer.py` but the exchange had a different signer from the UI's 1-click trading.
- **Why it wasn't caught earlier**: The old bot process (PID 45189) was killed and restarted on Mar 2 (during self-learning fix session). The previous process had the correct key loaded in memory from before the mismatch occurred.
- **Fix**: Re-linked our signer via `scripts/link_nado_signer.html` (MetaMask EIP-712 signing) + `submit_link_signer.py`. Required correct nonce (8) from Nado API. Also fixed the submit script payload format (`{link_signer: {tx: ...}}` not `{type: ...}`).
- **IMPORTANT**: The HTML file must be served via `python3 -m http.server` (localhost) for MetaMask to inject. Local `file://` doesn't work.

**Self-Learning Too Aggressive (Mar 8)** — Hibachi completely paralyzed:
- **Symptom**: 128 SELF-LEARN BLOCKED entries, 0 new trades for ~10 days
- **Root cause**: ALL 4 score buckets fell below 35% WR threshold with 8+ trades. Every signal was blocked.
  - 3.0-3.5: 33% WR (12 trades), 3.5-4.0: 33% (9), 4.0-4.5: 31% (16), 4.5-5.0: 33% (12)
- **Fix**: Loosened defaults in `self_learning.py`:
  - `score_bucket_block_wr`: 0.35 → **0.25** (only block truly terrible buckets)
  - `score_bucket_min_trades`: 8 → **15** (require more statistical evidence before blocking)
- Hibachi immediately started trading again after restart — BTC LONG, SUI SHORT, BNB SHORT placed

**Self-Learning Seeding Bugs (Mar 2)** — Fixed in `self_learning.py._load_history()`:
- Old trades stored `pnl_delta` but code only checked `pnl` → all old trades counted as 0 PnL (losses). Added `pnl_delta` fallback.
- Reconciled positions with `score=0` polluted bucket stats. Added guard to treat `score=0` as `None`.

**Position Tracking Gap** — Extended bot didn't detect manually-closed XMR position:
- Bot tracks positions internally (`self.position`). Manual close on exchange isn't detected.
- Workaround: restart the bot to re-reconcile from exchange.
- TODO: Add periodic exchange position check in `_manage_position()` to detect manual closes.

### Self-Learning System (updated Mar 8)
- Wired into all 3 bots via `core/strategies/momentum/self_learning.py` (`MomentumLearner` class)
- **Circuit Breaker**: 5 consecutive losses → 1h trading pause
- **Score Bucket Filter**: Tracks WR per score range, blocks buckets with <25% WR after 15+ trades (was 35%/8)
- Backtested on 497 real trades: circuit breaker + score bucket = net +$8.96 improvement
- Symbol blocker tested and REJECTED — blocked winners about to recover, net -$502

**Claude Code Version**: 2.1.62

### MCP Marketplace Integration (Feb 28)

**DeFi Rates MCP** — installed globally (`~/.claude.json`), free, no auth:
- Endpoint: `https://defiborrow.loan/mcp`
- 8 tools: `get_recent_events`, `get_whale_activity` (>$100k), `get_liquidations`, `get_lending_rates`, `get_earn_markets`, `find_best_borrow`, `find_best_yield`, `get_events_by_type`
- Covers: Aave, Morpho, Compound, Solend, Drift, Jupiter across Ethereum, Solana, Arbitrum, Base, BSC
- Use case: Liquidation cascades + whale activity as momentum signals

**MCP Marketplace** (https://mcpmarketplace.rickydata.org/) — 4,628 servers, 8,358 tools analyzed. Top candidates for future setup (paid, require wallet auth + USDC):
- **Aster Info** (12 tools) — Klines, funding rates, order book for Aster DEX (Solana perps)
- **Hyperliquid Info** (18 tools) — Full market data for largest perp DEX
- **Backtrader** (2 tools) — Strategy backtesting with Sharpe/drawdown metrics
- **Crypto Fear & Greed** (3 tools) — Regime filter for strategy activation
- **Crypto Orderbook** (2 tools) — CEX order book imbalance across 6 exchanges
- **Alpha Arena** (7 tools) — Hyperliquid execution + pre-computed technicals

**Strategy (v9 Engine — Signal-Based Exits, No Fixed TP/SL)**:
- 5-signal scoring: RSI + MACD + Volume + Price Action + EMA Trend (each 0-1, summed 0-5)
- **Unified config** — no per-exchange tuning, engine defaults for all:
  - score_min = 3.0 (only exchange-specific: offset_bps for hibachi, min_notional for nado)
  - max_positions = 5 (exchange margin naturally limits beyond this)
  - **Dynamic position sizing**: 20% of account equity per position (no hardcoded USD amounts)
  - **Exit: TREND_FLIP** (hold until scoring engine detects opposite direction with score >= 2.0)
  - **Exit: EMERGENCY_SL** (5% catastrophic stop — safety net only)
  - **No fixed TP** — let winners run until trend reverses
  - **No fixed max_hold** — hold as long as signal supports
- **Volume gate**: require vol_score > 0 (filters low-volume noise)
- 15-minute candles from Binance
- Momentum confirmation: last candle must agree with direction
- Position reconciliation on startup
- Maker-first exits (POST_ONLY then fallback to taker)

**v9 Engine Upgrade (Feb 17)**:
- **Problem**: Old engine used 4 signals with weighted average, threshold 0.4 — opened on any weak trend. 7 of 8 Extended positions were losing.
- **Solution**: Rewrote `engine.py` with v9-inspired 5-signal scoring from Alpha Arena winning strategy (+22.3% in 17 days).
- **Key changes**:
  - Added MACD(12,26,9) signal — crossover and histogram momentum
  - Added Price Action signal — support/resistance proximity
  - RSI thresholds match v9: <35/>65 = 1.0, 35-40/60-65 = 0.7, 40-45/55-60 = 0.3, 45-55 = 0.0
  - Volume thresholds match v9: >2x = 1.0, 1.5-2x = 0.7, 1.2-1.5x = 0.4
  - Direction by RSI+MACD confluence (was majority vote which caused 2v2 ties)
  - RSI neutral zone (45-55) doesn't vote — prevents false directional bias
  - Momentum confirmation: last 5m candle must agree with entry direction
    - Wider TP/SL: +1.5%/-1.0% (was +0.4%/-0.25%) to avoid noise stops
  - All 32 tests pass

**Tightening (Feb 17 afternoon)**:
- **Problem**: score_min=2.5 with unlimited positions caused Extended to open 9+ positions on weak signals (BERA 2.5, RESOLV 2.6, TRX 2.9). Bot maxed out $49 margin, then spammed 100+ failed order attempts per cycle. 4 of 9 positions closed as losses.
- **Fix 1**: Raised `score_min` from 2.5 to **3.0** — filters out ~80% of marginal signals
- **Fix 2**: Set `max_positions` from 0 (unlimited) to **5** per exchange — caps exposure
- **Fix 3**: Added position limit enforcement in `_cycle()` — `max_positions` field existed but was NEVER checked
- **Fix 4**: Added balance pre-check — if equity < 1.5x size_usd, skip entry silently (no error spam)
- **Result**: First cycle after restart — 0 balance errors (was 100+), all weak signals filtered

**Monitoring System (Feb 17)**:
- `scripts/monitor.py` — checks all exchanges every 5 minutes
- Reports: equity, positions, bot process status, alerts
- Logs to `monitor.log` + `monitor_snapshots.jsonl`
- Alerts on: bot death, negative equity

**Bug Fixes (Feb 17 afternoon)**:
- **Nado equity was -$477**: `get_equity()` incorrectly added `v_quote_balance` (cost basis) to spot balance. Fixed to use Nado's own PnL health calculation (`healths[2].assets`). Real equity: ~$40.
- **Extended close_position failed**: `get_all_positions()` returned market name "WIF-USD" but `close_position()` expected asset "WIF". Fixed symbol format.
- **Extended invalid quantity precision**: `close_position()` couldn't close non-BTC/ETH/SOL positions because size increments weren't loaded. Need to call `discover_markets()` first.
- **Bot doesn't reconcile existing positions**: On restart, bot starts with `position=None` and opens duplicate positions on top of existing ones. Added reconciliation at start of `_cycle()` — checks exchange for existing position and adopts it.
- **Hibachi position data wrong fields**: SDK returns `openPrice`/`unrealizedTradingPnl` but adapter used `entryPrice`/`unrealizedPnl`. Fixed.

**Extended Auto-Discovery (Feb 17)**:
- Extended upgraded from single-asset BTC to `--assets all` (58 crypto assets)
- Added `discover_markets()` to ExtendedAdapter via x10 SDK's `get_markets()`
- Dynamic size increment (`min_order_size_change`) and price tick (`min_price_change`) per asset
- Kilo-token mappings: `1000BONK` → Binance `BONKUSDT`, `1000PEPE` → `PEPEUSDT`, `1000SHIB` → `SHIBUSDT`
- Stocks (TSLA, NVDA, HOOD, COIN, etc.), commodities (XAU, XAG), forex (EUR) skipped — no Binance TA data
- 8 positions opened in first cycle (margin-limited with $62 equity)

**Auto-Discovery (Feb 16 evening)**:
- `--assets all` auto-discovers all markets from exchange API at startup
- Validates Binance TA data availability per asset (skips assets without klines)
- No more hardcoded symbol maps — bot reads what's available and trades it
- Hibachi: 7 markets, 6 tradeable (HYPE skipped — no Binance data)
- Nado: 26 markets, 20 tradeable (HYPE, MON, FARTCOIN, XAUT, USELESS, SKR skipped)
- Exchange margin limits naturally cap concurrent positions

**Multi-Asset Refactor (Feb 16 afternoon)**:
- Merged separate per-asset processes into single `--assets` process per exchange
- One process loops all assets per cycle, shares one adapter/connection
- PnL switched from balance-delta to calculated (entry/exit price * size)
- Hourly snapshots: one per cycle at account level (not per-bot)

**Bugs Fixed (Feb 16-17)**:
- `round(..., 2)` in engine killed precision for sub-$1 assets → adaptive rounding
- Nado adapter had hardcoded BTC price/size increments → dynamic from API
- Float→x18 conversion created non-divisible prices → SDK snaps to `price_increment_x18`
- POST_ONLY crossing guard added (ensures limit ≥1 tick from oracle)
- Floating point artifacts in price/size rounding → `round(..., 10)` after floor/ceil
- Extended `int(price)` killed sub-$1 assets → tick-based price rounding
- Extended `asset_precision` didn't match actual `min_order_size_change` → use size_inc from API
- Duplicate Extended/Paradex processes cleaned up

**Nado Performance (224 trades, Feb 16-17)**:
- 36.2% win rate (below ~39% breakeven for +40/-25 bps TP/SL)
- -$12.82 calculated PnL, spot balance dropped from ~$70 to ~$48
- SL exits (128) dominate: 2x TP exits (65)
- Worst: WLFI 6.7% WR, SUI 20%, kBONK 22.7%, PENGU 22.2%
- Best: ASTER 75%, PUMP 62.5%, UNI 60%, AAVE 55.6%
- Needs investigation: strategy tuning, per-asset performance tracking

### Nado Bug Bounty Program

**Program**: Nado Smart Contracts on HackenProof
**Scope**: https://github.com/nadohq/nado-contracts (Solidity 0.8.13, Ink L2, Vertex fork)
**Rewards**: $50 — $500,000
**Full analysis**: `research/nado/BUG_BOUNTY_ANALYSIS.md`

**Archive API Investigation Complete (500 matches analyzed)**:
- Total realized PnL: +$15.76, fees: $7.99, net: +$7.77
- Our fee tier: Taker 3.5 bps, Maker 1.0 bps (Entry tier, confirmed via API)

**Findings status**:
1. PnL settlement $0 — DEBUNKED (maker fills have 0 by design, taker fills correct)
2. v_quote_balance semantics — STILL VALID (needs more investigation)
3. POST_ONLY vs oracle mismatch — CONFIRMED but API gap, not contract bug
4. Fee overcharge on POST_ONLY — DEBUNKED (1 bps maker is documented Entry tier rate)
5. Minimum taker fee floor — DOCUMENTED BEHAVIOR ($100 × 0.035% = $0.035)
6. Fee rate discrepancy in Archive — INCONCLUSIVE (likely alpha promo rates)

**Remaining leads** (require Solidity PoC):
- `makerAccruesTakerFee()` exploitation path
- Cross-product liquidation cascade via low-liquidity oracle manipulation
- Builder fee validation edge cases

### Paradex GPT Swing Bot — Running

**Status**: LIVE, account $27.72 (started $27.91)

---

## Previous Status (2026-02-09)

### Multi-Timeframe Decision Engine Update

**Major Change**: Rebuilt decision engine with Moon Dev AI Agents pattern:

1. **Multi-timeframe analysis** (1H, 4H, 1D) - not just 1H RSI anymore
2. **Boolean signal matrix** for clarity (Price > MA20: ✅/❌)
3. **Crowd positioning data** from Binance Futures (L/S ratio, top traders, taker flow)
4. **Fear & Greed Index** as sentiment source (replacing Cambrian)
5. **Contrarian rules**:
   - Extreme Fear (<20) + Daily RSI Oversold (<30) = STRONG BUY
   - Extreme Greed (>80) + Daily RSI Overbought (>70) = STRONG SELL
   - Crowd heavily long (>60%) + price dumping = wait for capitulation

**Problem Solved**: Bot was shorting into rallies because it only saw 1H RSI 80 (overbought), missing that Daily RSI was 27 (OVERSOLD). Now sees full picture.

**Files Changed**:
- `orchestrator/simplified_decision_engine.py` - Complete rewrite with multi-TF
- `llm_agent/llm/model_client.py` - Added newer Qwen models (qwen3-235b, 10x cheaper)
- `scripts/paradex_gpt_live.py` - Fixed ceiling rounding for min notional

### Paper Test Results (24h - COMPLETED)

**Test Period**: Feb 6-7, 2026 (24 hours)
**Market Condition**: BTC dumped from $70.7k to $69.4k

| Test | Engine | Trades | Win% | P&L | Return% |
|------|--------|--------|------|-----|---------|
| Current_B | current | 1 | 0% | -$1.76 | **-0.88%** |
| Simplified_A | simplified | 1 | 0% | -$1.94 | -25.36% |
| Simplified_B | simplified | 1 | 0% | -$1.94 | -25.36% |
| Current_A | current | 2 | 0% | -$3.42 | -30.51% |

**Finding**: Conservative engine (fewer trades) outperformed during dump. The "buy the fear" contrarian thesis needs more time or was wrong for this cycle.

### Paradex Live Bot - RUNNING

**Status**: LIVE with open BTC LONG position

| Field | Value |
|-------|-------|
| Position | LONG BTC |
| Size | 0.00015 BTC (~$10.61) |
| Entry | $70,717.84 |
| Current Price | ~$69,400 |
| Unrealized PnL | **-$0.15** (-1.4%) |
| TP Target | $80,302 (+14%) |
| SL Target | $60,000 (-15%) |
| Account Value | $27.91 |

**Command**:
```bash
nohup python3.11 -u scripts/paradex_gpt_live.py --live --model qwen-max --interval 300 --size 15 > logs/paradex_live_v2.log 2>&1 &
```

### Funding Rate Analysis (2026-02-09 12:00)

| Symbol | Funding Rate | Annualized | Interpretation |
|--------|--------------|------------|----------------|
| BTC | -0.0033% | -3.6%/yr | Slightly negative |
| ETH | -0.0005% | -0.6%/yr | Neutral |
| SOL | -0.0042% | -4.6%/yr | Slightly negative |

**Crowd Positioning (BTC)**:
- Retail L/S: 1.69 (62.8% long) - 🔴 Crowd heavily long
- Top Traders: 1.31 (56.7% long) - Moderately long
- Taker Flow: 1.09 (Buyers slightly dominating)

**Key Insight**: Funding just flipped positive (+0.0018%) after being negative for days. Crowd still 62.8% long while price dumps - more liquidation fuel possible, but shorts starting to cover.

### Bugs Fixed (2026-02-06)

1. **Minimum Order Size Bug**:
   - Problem: Engine calculated max_trade_size=$7 but min_order=$10
   - Fix: `max_trade_size = max(max_trade_size, min_order)` in simplified_decision_engine.py

2. **Ceiling Rounding Bug**:
   - Problem: `round(size, 5)` dropped $10 order to $9.89 (below minimum)
   - Fix: `math.ceil(size * 100000) / 100000` in paradex_gpt_live.py

3. **Multi-Timeframe RSI**:
   - Problem: Only sending 1H RSI to LLM, missing Daily/4H context
   - Fix: Fetch all timeframes, format as signal matrix for LLM

---

## Currently Running

### Grid Market Making (v12 - Dynamic Spread + POST_ONLY)

#### Paradex Grid MM (BTC-USD-PERP)
**Status**: RUNNING (v12 Dynamic Spread + POST_ONLY)
**Script**: `scripts/grid_mm_live.py`
**Log**: `logs/grid_mm_live.log`
**Python**: 3.11 (required for paradex-py with ParadexSubkey)

**Current Performance (2026-01-22)**:
- Account Value: $92.68
- Position: SHORT 0.00078 BTC
- 24h P&L: -$0.01 (100 trades)

**Configuration (v12 - Dynamic Spread + POST_ONLY)**:
- Symbol: BTC-USD-PERP
- Spread: **DYNAMIC** based on ROC:
  - ROC 0-5 bps → 1.5 bps spread (calm market)
  - ROC 5-15 bps → 3 bps spread (low volatility)
  - ROC 15-30 bps → 6 bps spread (moderate volatility)
  - ROC 30-50 bps → 10 bps spread (high volatility)
  - ROC >50 bps → PAUSE orders
- Order type: **POST_ONLY** (maker-only, reject if would cross spread)
- Order size: $100/order
- Levels: 2 per side
- Max inventory: 100%
- Capital: Dynamic (from exchange balance)

**Start**:
```bash
nohup python3.11 scripts/grid_mm_live.py > logs/grid_mm_live.log 2>&1 &
```

#### Nado Grid MM (ETH-PERP) + LLM Trading (BTC/SOL)
**Status**: RUNNING (v19 Grid MM + LLM Trading)
**Script**: `scripts/grid_mm_nado_v8.py`
**Log**: `logs/grid_mm_nado.log`

**Current Performance (2026-01-28)**:
- Balance: $40.52
- Position: LONG 0.009 ETH (~$27)
- **Maker Rate: 100%** (POST_ONLY working)
- Rebalance threshold: 55%

**Configuration (v19 - Grid MM + LLM Trading)**:

*Grid MM (ETH-PERP)*:
- Symbol: ETH-PERP
- Spread: **DYNAMIC** based on ROC (Qwen-calibrated):
  - ROC 0-5 bps → 4 bps spread (calm market)
  - ROC 5-10 bps → 6 bps spread (low volatility)
  - ROC 10-20 bps → 8 bps spread (moderate volatility)
  - ROC 20-30 bps → 12 bps spread (high volatility)
  - ROC 30-50 bps → 15 bps spread (very high volatility)
  - ROC >50 bps → PAUSE orders
- Order type: **POST_ONLY** (maker-only)
- Order size: $100/order (Nado minimum)
- Levels: 2 per side
- Max inventory: **400%** (4x leverage - needed for $100 orders on $40 balance)
- Capital: Dynamic (from exchange balance)

*LLM Trading (BTC-PERP, SOL-PERP)*:
- Model: qwen/qwen-2.5-72b-instruct (via OpenRouter)
- Position size: $25
- Max positions: 2
- Check interval: 600s (10 minutes)
- Exit rules: +2.0% TP / -1.5% SL / 4h max hold

**Start**:
```bash
nohup python3 scripts/grid_mm_nado_v8.py > logs/grid_mm_nado.log 2>&1 &
```

**Grid MM v18 Strategy (Qwen-Calibrated 2026-01-22)**:
Dynamic spread calibrated by Qwen between two failed extremes:
- v12 (1.5 bps calm) → 500 trades/7d but -$23.57 from adverse selection (-8.5 bps avg)
- v13 (15 bps calm) → 0 fills in 5+ hours, too wide for anyone to hit
- v18 (4 bps calm) → middle ground, should get fills while reducing adverse selection
Removed tight_spread_mode (redundant with proper dynamic bands). PAUSE all orders when ROC exceeds 50 bps. All orders use POST_ONLY to guarantee maker fills only.

Note: Bots need 3 minutes to build price history before ROC activates.

**Stop all Grid MM**:
```bash
pkill -f grid_mm_live && pkill -f grid_mm_nado
```

---

### LLM Directional Bots (Strategy D - Delta Neutral Pairs)

#### Hibachi Bot - Strategy D
**Status**: NOT RUNNING (paused for grid MM focus)
**Script**: `hibachi_agent/bot_hibachi.py --strategy D`
**Log**: `logs/hibachi_bot.log`

**Configuration**:
- Strategy: Delta Neutral Pairs Trade
- LLM picks direction (long stronger asset, short weaker)
- Hold: 1 hour then close both legs
- Pairs: BTC/ETH

**Start**:
```bash
nohup python3 -u -m hibachi_agent.bot_hibachi --live --strategy D --interval 600 > logs/hibachi_bot.log 2>&1 &
```

#### Extended Grid MM (BTC-USD)
**Status**: RUNNING (v18 POST_ONLY Maker-Only)
**Script**: `scripts/grid_mm_extended.py`
**Log**: `logs/grid_mm_extended.log`

**Current Performance (2026-01-23)**:
- Balance: $65.01
- Position: FLAT
- Maker Fee: **0.000%** (FREE!)
- Taker Fee: 0.025% (what we were paying before)

**Configuration (v18 - POST_ONLY Grid MM)**:
- Symbol: BTC-USD
- Spread: **DYNAMIC** based on ROC (Qwen-calibrated):
  - ROC 0-5 bps → 4 bps spread (calm market)
  - ROC 5-10 bps → 6 bps spread (low volatility)
  - ROC 10-20 bps → 8 bps spread (moderate volatility)
  - ROC 20-30 bps → 12 bps spread (high volatility)
  - ROC 30-50 bps → 15 bps spread (very high volatility)
  - ROC >50 bps → PAUSE orders
- Order type: **POST_ONLY** (maker-only, 0% fees)
- Order size: $50/order
- Levels: 2 per side
- Max inventory: 100%
- Refresh: 5 minutes or 0.5% price move

**Start**:
```bash
nohup python3.11 -u scripts/grid_mm_extended.py > logs/grid_mm_extended.log 2>&1 &
```

---

## Exchange Accounts (2026-02-09)

| Exchange | Balance | Bot Running | Notes |
|----------|---------|-------------|-------|
| Paradex | $27.91 | GPT Swing (BTC LONG) | Running since Feb 7 |
| Hibachi | $64.87 | Momentum (BTC, 18bps) | Started Feb 9 14:00 |
| Nado | $81.94 | Momentum (BTC, 8bps POST_ONLY) | Started Feb 9 14:57, $105/trade |
| Extended | $60.54 | Momentum (BTC, 8bps POST_ONLY) | Started Feb 9 14:02 |
| **Total** | **$235.26** | **4 bots live** | |

---

## Recent Changes (2026-01-16)

### CRITICAL: POST_ONLY Order Fix
- **Problem**: Grid bots were paying 3.5x higher fees due to taker fills
- **Discovery**: Nado had only 58% maker rate - 42% of trades were taker
- **Root Cause**:
  - Nado: `order_type="LIMIT"` mapped to DEFAULT (can cross spread)
  - Paradex: No instruction set, defaulted to GTC (can cross spread)
- **Fix**:
  - Nado: Changed to `order_type="POST_ONLY"`
  - Paradex: Added `instruction="POST_ONLY"` to Order() calls
- **Result**: Nado now showing **100% maker rate**
- **Impact**: ~65% reduction in trading fees

### P&L Tracking via Exchange API
- Added `get_pnl()` method to Nado SDK using Archive API
- Uses `matches` endpoint with `realized_pnl` and `fee` fields
- Validated: SDK returns accurate P&L matching exchange UI
- **Rule**: Never trust bot-calculated P&L, always use exchange API

### Paradex Python Version
- Paradex requires Python 3.11 for `ParadexSubkey` class
- Python 3.9's paradex-py removed this class in newer versions
- Updated start command to use `python3.11`

---

## Recent Changes (2026-01-15)

### Grid MM v12 - Dynamic Spread Implementation
- Implemented automatic spread adjustment based on ROC volatility
- Spread bands:
  - ROC 0-5 bps → 1.5 bps spread (calm market, max fills)
  - ROC 5-15 bps → 3 bps spread (low volatility)
  - ROC 15-30 bps → 6 bps spread (moderate volatility)
  - ROC 30-50 bps → 10 bps spread (high volatility)
  - ROC >50 bps → PAUSE orders (existing logic)
- Applied to both Paradex (`grid_mm_live.py`) and Nado (`grid_mm_nado_v8.py`)
- Added comprehensive tests: `tests/test_dynamic_spread.py` (25 tests passing)
- Logs show spread changes: `SPREAD WIDENED: 1.5 → 3.0 bps (ROC: +7.2)`

### Strategy D Pairs Trade Bug Fix
- Fixed bug where hard exit rules were closing individual pairs trade legs
- Root cause: Main bot loop applied "CUT LOSER" rule to all positions
- Fix: Skip hard exit rules for Strategy D pairs positions (`hibachi_agent/bot_hibachi.py:439-455`)
- Both legs now close together through Strategy D logic

---

## Nado DEX Integration (2026-01-12)

**Status**: SDK COMPLETE, BOT RUNNING

### Setup Complete
- Generated linked signer key: `0xd086A7a803f23a4C714e01d67e0f733851431827`
- Authorized via EIP-712 LinkSigner signature
- Credentials in `.env`: `NADO_WALLET_ADDRESS`, `NADO_LINKED_SIGNER_PRIVATE_KEY`, `NADO_SUBACCOUNT_NAME`

### SDK Features Working
- `get_products()` - Lists all perp products
- `get_balance()` - USDT0 balance
- `get_positions()` - Open positions
- `get_pnl(hours)` - P&L from Archive API (realized_pnl + fees)
- `create_market_order()` - IOC orders with aggressive pricing
- `create_limit_order()` - Limit orders with POST_ONLY support
- `verify_linked_signer()` - Auth verification

### Key Implementation Notes
1. **Verifying Contract**: For orders, use `address(productId)` not endpoint address
2. **Nonce Format**: `(recv_time_ms << 20) + random_bits` - recv_time is FUTURE timestamp
3. **Price**: Must be within 20-500% of oracle, divisible by price_increment
4. **Market Orders**: Use IOC with aggressive price (200% oracle for buys, 50% for sells)
5. **POST_ONLY**: Use for grid MM to guarantee maker fills

### Files
- SDK: `dexes/nado/nado_sdk.py`
- Docs: `research/nado/API_SIGNING.md`, `research/nado/API_PLACE_ORDER.md`
- Link Signer Tool: `scripts/link_nado_signer.html`, `scripts/submit_link_signer.py`

### Nado Chain Info
- **Mainnet**: Chain ID 57073 (Ink L2)
- **Gateway**: `https://gateway.prod.nado.xyz/v1`
- **Archive API**: `https://archive.prod.nado.xyz/v1`
- **Docs**: https://docs.nado.xyz/developer-resources/api

---

## Key Files

| Purpose | File |
|---------|------|
| Paradex Grid MM | `scripts/grid_mm_live.py` |
| Nado Grid MM | `scripts/grid_mm_nado_v8.py` |
| Hibachi Grid MM | `scripts/grid_mm_hibachi.py` |
| Hibachi LLM Executor | `hibachi_agent/execution/hibachi_executor.py` |
| Real P&L Tracker | `scripts/pnl_tracker.py` |
| Hibachi SDK | `dexes/hibachi/hibachi_sdk.py` |
| Nado SDK | `dexes/nado/nado_sdk.py` |
| Dynamic Spread Tests | `tests/test_dynamic_spread.py` |
| Momentum Engine | `core/strategies/momentum/engine.py` |
| Momentum Adapters | `core/strategies/momentum/exchange_adapter.py` |
| Momentum Bot | `scripts/momentum_mm.py` |
| Momentum Backtest | `scripts/backtest_momentum.py` |
| Momentum Tests | `tests/test_momentum.py` |

---

## Philosophy

**Grid Market Making**:
- Earn spread by providing liquidity
- Pause during strong trends (ROC detection)
- Use leverage for capital efficiency
- **Always use POST_ONLY** to guarantee maker fills

**LLM Swing Trading** (when enabled):
- 6 trades/day max
- 8% TP, 4% SL (2:1 R/R minimum)
- 48h max hold
- Cut losers after 4h if underwater


---

## Nado Signer Setup Reference

**Two signers have existed**:
- `0x36e82b22...` — Nado UI's 1-click trading auto-generated signer
- `0xd086A7a8...` — Our generated signer (in `.env` as `NADO_LINKED_SIGNER_PRIVATE_KEY`)

**IMPORTANT**: The Nado UI's 1-click trading generates its OWN signer. To use OUR signer, must link via EIP-712:
1. Serve `scripts/link_nado_signer.html` via `python3 -m http.server 8888 --directory scripts/`
2. Open `http://localhost:8888/link_nado_signer.html` in browser with MetaMask
3. Connect wallet, set correct nonce, sign
4. Submit via API with correct payload format: `{"link_signer": {"tx": {...}, "signature": "..."}}`

**If signer breaks again**: Run `verify_linked_signer()` to check mismatch, then re-link via HTML tool.

**Withdrawal issue (2026-01-21)**: Linked signers cannot withdraw — need main wallet + native ETH on INK chain for gas.

---

## Hibachi Strategy Change (2026-01-21)

**Changed**: Strategy D Pairs Trade → Grid MM

**Why**:
- Strategy D was bleeding money (state sync issues, 0.045% taker fees)
- Grid MM uses limit orders = 0% maker fees
- Better volume farming for points

**Config**:
```
Script: scripts/grid_mm_hibachi.py
Symbol: BTC/USDT-P
Spread: 20 bps (wide to ensure maker)
Order Size: $100
Levels: 2 per side
Refresh: 30s
```

**Command**: `nohup python3 -u scripts/grid_mm_hibachi.py --spread 20 --size 100 --levels 2 > logs/grid_mm_hibachi.log 2>&1 &`

---

## Hibachi Dual Strategy (2026-01-22)

**Architecture**: Grid MM (BTC) + LLM Directional (all other assets) on same account

**How It Works**:
- Grid MM (BTC only): Earns spread, automated, 30s refresh
- LLM Bot (ETH, SOL, SUI, XRP, DOGE): Qwen scans all markets, picks best setup

**Asset Isolation**:
| Script | Assets | Purpose |
|--------|--------|---------|
| grid_mm_hibachi.py | BTC/USDT-P | Spread capture |
| hibachi_agent.bot_hibachi | All except BTC | LLM directional |

**Start Commands**:
```bash
# Grid MM (BTC)
nohup python3 -u scripts/grid_mm_hibachi.py > logs/grid_mm_hibachi.log 2>&1 &

# LLM Directional (Strategy F - Self-Improving)
nohup python3 -u -m hibachi_agent.bot_hibachi --live --strategy F --interval 600 > logs/hibachi_bot.log 2>&1 &
```

**Stop Commands**:
```bash
pkill -f grid_mm_hibachi
pkill -f bot_hibachi
```

**Monitor**:
```bash
tail -f logs/grid_mm_hibachi.log logs/hibachi_bot.log
```

---

## Fixes Applied (2026-01-22 Evening)

### 1. Hibachi LLM Bot: Maker-Only Orders
**Problem**: LLM bot used `create_market_order` (taker fees on every trade)
**Fix**: Modified `hibachi_agent/execution/hibachi_executor.py`:
- Added `maker_only=True` parameter
- New `_get_aggressive_limit_price()` method: places limit orders 40% into spread
- Both open and close use limit orders now
- Fee rate: 0.0 when maker_only (was 0.00035)

### 2. Nado Rebalance Threshold Fix
**Problem**: Rebalance never triggered (inventory at 62% of leveraged max, below 95% threshold)
**Root cause**: `max_inventory_pct=175%` → max_inventory=$109.65 → actual ratio=62% < 95%
**Fix**: Changed `rebalance_threshold_pct` from 95.0 to 55.0 in `scripts/grid_mm_nado_v8.py`
**Result**: Rebalance now triggers correctly, confirmed in logs

### 3. P&L Tracker Created
**Problem**: Dashboard (`hibachi_dashboard.py`) showed fabricated +$419 profit
**Fix**: Deleted dashboard, created `scripts/pnl_tracker.py` that queries real exchange APIs
**Usage**: `python3 scripts/pnl_tracker.py`

### 4. CLAUDE.md Updated
Added critical rule: NEVER trust dashboard/local tracking for P&L. Always query exchange APIs directly.

---

## LLM Supervisor Fix (2026-01-22)

**Problem**: Supervisor never traded for 24+ hours because `get_market_data()` returned PLACEHOLDER values:
```python
'rsi': 50.0,  # HARDCODED - always neutral
'macd': 0.0,  # HARDCODED - always flat
```
Result: Score was always 0.5/5.0 → never met 3.0 threshold.

**Fix Applied**: Integrated `HibachiMarketDataAggregator` to fetch real indicators:
```python
# Now uses real data from Binance proxy + indicator calculator
ETH/USDT-P: RSI=41.1, MACD=-5.3523, Vol=0.77x
SOL/USDT-P: RSI=53.9, MACD=0.0185, Vol=0.48x
```

**Files Changed**:
- `scripts/llm_supervisor_hibachi.py` - Imported aggregator, updated `get_market_data()`

**Verification**: Supervisor now logs real indicator values and makes informed trading decisions.

---

## Hibachi Improvements (2026-01-22)

**HIB-001 to HIB-007 COMPLETE**

### New Features Implemented:

1. **Momentum Confirmation (HIB-001)**: 5-minute momentum must match LLM direction before entry

2. **Trailing Stop (HIB-002)**:
   - At +4% P&L: Breakeven lock activated
   - At +6% P&L: Trail at (current - 2%)

3. **Optimized Spreads (HIB-003)**: Reduced all spread bands by 20%
   - Calm: 8 bps (was 10)
   - Low vol: 12 bps (was 15)
   - Moderate: 16 bps (was 20)
   - High vol: 20 bps (was 25)

4. **Win Rate Tracking (HIB-004)**: Auto-block symbols with <30% win rate over 10+ trades

5. **Caching (HIB-005)**: 60s indicator cache, skip LLM if no significant change

6. **Conviction Sizing (HIB-006)**:
   - 0.7-0.8 conf: 1x base size
   - 0.8-0.9 conf: 1.5x
   - 0.9+ conf: 2x

7. **P&L Tracker (HIB-007)**: `python3 scripts/pnl_tracker.py` (queries real exchange APIs)

### Commands:

```bash
# Grid MM (BTC)
nohup python3 -u scripts/grid_mm_hibachi.py > logs/grid_mm_hibachi.log 2>&1 &

# LLM Directional (improved, MAKER-ONLY)
nohup python3 -u -m hibachi_agent.bot_hibachi --live --strategy F --interval 600 > logs/hibachi_bot.log 2>&1 &

# Real P&L check
python3 scripts/pnl_tracker.py
```

---

## Changes (2026-01-31)

### Machine Restart Recovery
- Full machine restart at ~09:27
- All 6 bots restarted successfully
- Running 6+ hours since restart

### VPS Deployment Planning
- Deep repo analysis completed: 334MB total, 306MB logs (92%)
- Found 49+ dead scripts to delete, duplicate code to consolidate
- Missing requirements: pandas, numpy, ta, eth_account, paradex_py
- Plan created for 4-phase cleanup before Hetzner deployment

---

## Changes (2026-01-30)

### XCU-USD Added to Extended
- Added XCU-USD (new listing) to Extended whitelist for points boost
- Extended LLM (Strategy D) now trades: BTC-USD, ETH-USD, SOL-USD, XCU-USD
- Added XCU-USD market config to grid_mm_extended.py
- XCU params: ~$6 price, 1 decimal precision, $6 min order

### Computer Sleep Recovery
- Hibachi LLM had connection errors after computer woke from sleep
- Restarted Hibachi LLM bot - now running normally

---

## Fixes Applied (2026-01-28)

### 1. Nado Grid MM Not Placing Orders
**Problem**: Nado bot was running but not placing any orders for hours
**Root Cause**: With $40 balance and 175% max_inventory ($70 max), a $100 order on top of existing $27 position ($127 total) exceeded limit
**Fix**: Increased `max_inventory_pct` from 175% to 400% (4x leverage) in `scripts/grid_mm_nado_v8.py:1419`
**Result**: Grid orders now placing successfully (4 orders: 2 BUY + 2 SELL)

### 2. LLM Trading Added to Nado
**Feature**: Nado now has LLM-based directional trading alongside Grid MM
**Assets**: BTC-PERP, SOL-PERP (separate from ETH-PERP grid)
**Config**: $25 positions, 2 max, +2%/-1.5%/4h exits, Qwen model
**Implementation**: Direct OpenRouter API calls via aiohttp (no llm_agent dependency)

### 3. Hibachi Trade Tracking Fix
**Problem**: Trade P&L tracking showed +$10.91 discrepancy vs actual equity
**Root Cause 1**: CLOSE actions in main decision flow never called `record_exit()` - only LONG/SHORT had recording
**Fix 1**: Added `record_exit()` call for CLOSE actions in `hibachi_agent/bot_hibachi.py`
**Root Cause 2**: Orphan positions (exist on exchange but not in tracker) caused missing P&L
**Fix 2**: Added `sync_orphan_positions()` method that runs on startup to detect and create synthetic entries

### 4. LLM Bot SELL Action Bug (All Bots)
**Problem**: LLM returning "SELL" for assets was being rejected instead of closing positions
**Root Cause**: SELL action was checked against current positions and rejected if no SHORT existed
**Fix**: Convert SELL → CLOSE when there's a LONG position to close
**Applied to**: hibachi, lighter, extended, pacifica bots

---

## Swing Trading Orchestrator (2026-02-03)

**NEW STRATEGY**: Swing trading orchestrator with funding rate exploitation

### Architecture

```
SWING_ORCHESTRATOR (15-min cycles)
    ├── FundingMonitor (Binance reference rates)
    ├── TechnicalEngine (5-signal scoring: RSI, MACD, OI, Volume, EMA)
    ├── LLMDecisionEngine (Qwen - dynamic position sizing)
    ├── PositionManager (entry/exit/trailing stops)
    └── Exchange Agents
        ├── ParadexAgent (BTC, ETH) - needs Python 3.11
        ├── HibachiAgent (all assets)
        ├── NadoAgent (BTC, ETH, SOL) - $100 min order
        └── ExtendedAgent (BTC, ETH, SOL, XCU)
```

### Key Features

1. **LLM-DRIVEN DECISIONS** (Qwen via OpenRouter)
   - LLM decides: trade or not, direction, AND position size
   - Position size based on LLM's conviction (not hardcoded percentages)
   - User requirement: "Let it choose whatever size based on conviction"
   - As account grows ($100 → $500), strategy adapts dynamically
2. **15-minute decision cycles** (not scalping, not too slow)
3. **Funding rate signals**:
   - Extreme positive (>0.03%): STRONG LONG (squeeze potential)
   - Extreme negative (<-0.03%): STRONG SHORT (liquidation potential)
4. **5-signal technical scoring** (0-5 scale) - provided to LLM as context
5. **SHORT bias** hint provided to LLM (49.4% vs 41.8% historical WR)
6. **Accurate logging**: Exchange API is source of truth, NEVER local calculations

### CRITICAL: LLM-Driven Position Sizing

**NO MORE HARDCODED SIZES**. The old approach:
```python
# BAD - don't do this:
SIZE_HIGH_CONVICTION = 0.20  # 20%
SIZE_STANDARD = 0.10         # 10%
size_usd = balance * SIZE_STANDARD  # WRONG
```

**NEW APPROACH** (implemented 2026-02-03):
```python
# GOOD - LLM decides:
decision = await llm_engine.get_decision(
    balances=balances,       # Full account context
    positions=positions,     # What we already hold
    funding_data=funding,    # Funding rates
    technical_data=technicals # RSI, MACD, OI, etc.
)
# LLM returns: decision, direction, size_usd (its choice), conviction, reasoning
```

The LLM gets complete market context and chooses position size based on:
- Its conviction level (HIGH/MEDIUM/LOW)
- Account balance available
- Risk appetite implied by technicals
- Exchange minimums (validated, not forced)

### Exit Rules

| Trade Type | Take Profit | Stop Loss | Time Stop |
|------------|-------------|-----------|-----------|
| High Conviction | +5% | -2% | 48h |
| Standard Swing | +3% | -1.5% | 24h |
| Scalp | +1.5% | -1% | 2h |

### Trailing Stop
- At +4% P&L: Move stop to breakeven
- At +6% P&L: Trail at (current - 2%)

### Files

| File | Purpose |
|------|---------|
| `orchestrator/swing_orchestrator.py` | Main orchestrator |
| `orchestrator/llm_decision_engine.py` | **LLM-driven decisions (dynamic sizing)** |
| `orchestrator/funding_monitor.py` | Funding rate tracking |
| `orchestrator/technical_engine.py` | 5-signal scoring |
| `orchestrator/position_manager.py` | Position management |
| `orchestrator/agents/*.py` | Exchange agents |
| `orchestrator/test_harness.py` | Validation & testing |
| `orchestrator/config.py` | Configuration (note: position sizes now LLM-driven) |

### Commands

```bash
# Pre-flight checks
python3 -m orchestrator.test_harness --mode preflight

# Single cycle test
python3 -m orchestrator.test_harness --mode single

# Start 24-hour live test
python3 -m orchestrator.test_harness --mode 24h

# Or run directly
python3 -m orchestrator.swing_orchestrator
```

### Log Files

- `logs/swing_orchestrator.log` - Main log
- `logs/swing_decisions.jsonl` - All decisions (machine-readable)
- `logs/swing_positions.jsonl` - Position snapshots
- `logs/swing_funding.jsonl` - Funding rate history
- `logs/swing_pnl.jsonl` - P&L tracking

### PRD Location

`.taskmaster/docs/swing_orchestrator_prd.txt`

---

## Current Bot Status (2026-02-09)

| Bot | Asset | Strategy | Status |
|-----|-------|----------|--------|
| **Paradex GPT Live** | BTC | Multi-TF Swing (Qwen) | 🟢 RUNNING (LONG BTC) |
| **Hibachi Momentum** | BTC | Momentum Limit Orders (18bps offset) | 🟢 RUNNING |
| **Nado Momentum** | BTC | Momentum Limit Orders (8bps POST_ONLY) | 🟢 RUNNING |
| **Extended Momentum** | BTC | Momentum Limit Orders (8bps POST_ONLY) | 🟢 RUNNING |
| Paper Test | BTC, ETH, SOL | A/B Test Current vs Simplified | ✅ COMPLETED |
| Hibachi LLM Bot | ETH, SOL, SUI, XRP, DOGE | Strategy F (MAKER-ONLY) | ⏸️ PAUSED |
| Hibachi Grid MM | BTC | Spread capture | ⏸️ PAUSED |
| Nado Grid MM + LLM | ETH (grid) + BTC/SOL (LLM) | v19 Grid + LLM directional | ⏸️ PAUSED |
| Paradex Grid MM | BTC | Spread capture | ⏸️ PAUSED |
| Extended LLM | BTC, ETH, SOL, XCU | Strategy D pairs trade | ⏸️ PAUSED |

**Exchange Balances at Momentum Bot Start (2026-02-09 ~14:00):**

| Exchange | Starting Equity | Bot | Notes |
|----------|----------------|-----|-------|
| Paradex | $27.91 | GPT Swing (separate) | LONG BTC position |
| Hibachi | **$64.87** | Momentum (18bps, no POST_ONLY) | $50/trade |
| Nado | **$81.94** | Momentum (8bps, POST_ONLY) | $105/trade (Nado $100 min) |
| Extended | **$60.54** | Momentum (8bps, POST_ONLY) | $50/trade |
| **Total** | **$235.26** | 4 bots live | |

---

### Momentum Limit Order Bot (DEPLOYED 2026-02-09)

**Strategy**: Detect 5m trend → POST_ONLY limit order behind price → price pulls back → fills → continues → TP/SL exit

**Architecture**: One script per exchange. `--assets all` auto-discovers markets:
```bash
python3 scripts/momentum_mm.py --exchange hibachi --assets all --interval 60   # auto-discover
python3 scripts/momentum_mm.py --exchange nado --assets all --interval 60      # auto-discover
python3 scripts/momentum_mm.py --exchange nado --assets BTC,DOGE --interval 60 # explicit list
python3 scripts/momentum_mm.py --exchange extended --asset BTC --interval 60   # single asset
```

**Core Logic** (`core/strategies/momentum/engine.py`):
- 4-signal trend scoring: ROC (35%), EMA slope (30%), RSI zone (20%), Volume (15%)
- Trend threshold: 0.4 strength minimum
- Entry: POST_ONLY limit offset behind price (8bps Nado/Extended, 18bps Hibachi)
- Exit: TP +40bps / SL -25bps / Time 60min
- Cooldown: 5min between trades
- Fixed size: $50 (Hibachi/Extended), $105 (Nado — $100 min notional)
- **No martingale**, no confidence scaling

**PnL Tracking**: Calculated per-position (entry/exit price * size). Balance-delta stored as secondary field for account-level tracking.

**Backtest Results** (Binance 5m klines, 30 days):

| Asset | Offset | Trades | Win% | P&L | Notes |
|-------|--------|--------|------|-----|-------|
| BTC | 18bps | 804 | 49.5% | +$19.10 | Hibachi config |
| BTC | 8bps | 1,230 | 47.5% | +$23.00 | Nado/Extended config |
| ETH | 8bps | 1,424 | 49.3% | +$33.10 | Future expansion |
| SOL | 8bps | 1,465 | 53.7% | +$50.00 | Future expansion |

**Tests**: 31/31 passing (`tests/test_momentum.py`)

**Files**:

| File | Purpose |
|------|---------|
| `core/strategies/momentum/engine.py` | MomentumEngine: trend detection, entry/exit calc |
| `core/strategies/momentum/exchange_adapter.py` | Unified adapter for Hibachi/Nado/Extended |
| `scripts/momentum_mm.py` | Main bot script (CLI, logging, main loop) |
| `tests/test_momentum.py` | 31 tests (trend, entry, TP/SL, cooldown, PnL) |
| `scripts/backtest_momentum.py` | Backtester using Binance klines |

**Logs** (`logs/momentum/`):
- `{exchange}_bot.log` — Runtime log
- `{exchange}_trades.jsonl` — Trade entries/exits with balance-delta PnL
- `{exchange}_audit.csv` — Equity snapshots (PRE_OPEN, POST_CLOSE, BOT_START)
- `{exchange}_hourly.jsonl` — Hourly equity snapshots

**Commands**:
```bash
# Start momentum bots (Hibachi + Nado auto-discover all markets)
nohup python3 -u scripts/momentum_mm.py --exchange hibachi --assets all --interval 60 > logs/momentum/hibachi_bot.log 2>&1 &
nohup python3 -u scripts/momentum_mm.py --exchange nado --assets all --interval 60 > logs/momentum/nado_bot.log 2>&1 &
nohup python3 -u scripts/momentum_mm.py --exchange extended --asset BTC --interval 60 > logs/momentum/extended_bot.log 2>&1 &

# Monitor
tail -f logs/momentum/hibachi_bot.log logs/momentum/nado_bot.log logs/momentum/extended_bot.log

# Stop all momentum bots
pkill -f momentum_mm

# Check trades
cat logs/momentum/hibachi_trades.jsonl
cat logs/momentum/nado_trades.jsonl
cat logs/momentum/extended_trades.jsonl
```

**Known Issue (Fixed)**: Nado requires $100 minimum notional. Initial $50 size failed with error code 2094. Fixed by setting `config.size_usd = 105.0` for Nado.

---

**Active Bots (2026-02-16)**:
1. **Hibachi Momentum** — ALL 6 assets (auto-discovered), 18bps offset, $50/trade
2. **Nado Momentum** — ALL 20 assets (auto-discovered), 8bps POST_ONLY, $105/trade
3. **Extended Momentum** — BTC, 8bps POST_ONLY, $50/trade
4. **Paradex GPT Swing** — BTC, Qwen multi-TF, $15/trade

**Commands:**
```bash
# Start all bots (Hibachi + Nado auto-discover all markets)
nohup python3 -u scripts/momentum_mm.py --exchange hibachi --assets all --interval 60 > logs/momentum/hibachi_bot.log 2>&1 &
nohup python3 -u scripts/momentum_mm.py --exchange nado --assets all --interval 60 > logs/momentum/nado_bot.log 2>&1 &
nohup python3 -u scripts/momentum_mm.py --exchange extended --asset BTC --interval 60 > logs/momentum/extended_bot.log 2>&1 &
nohup python3.11 -u scripts/paradex_gpt_live.py --live --model qwen-max --interval 300 --size 15 > logs/paradex_live_v2.log 2>&1 &

# Monitor all
tail -f logs/momentum/nado_bot.log logs/momentum/hibachi_bot.log logs/momentum/extended_bot.log logs/paradex_live_v2.log

# Stop all
pkill -f momentum_mm && pkill -f paradex_gpt_live
```
