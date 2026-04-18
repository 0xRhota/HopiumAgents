# Scratchpad — Circle Back Items

## ⚠️ NADO SIGNER WARNING — READ THIS ⚠️

**DO NOT toggle 1-Click Trading on the Nado UI (app.nado.xyz).** Every time you enable/disable it, Nado replaces our bot's linked signer with their auto-generated one, and the bot silently stops placing orders (error 2028). This has happened 3 times now (Mar 2, Mar 13, Mar 22).

**When logging into Nado UI**: Just log in normally. Do NOT touch Settings → 1-Click Trading. If it asks you to enable it, DECLINE.

**If it breaks again**: Tell Claude "nado signer broke" — the fix takes ~2 minutes (re-link via MetaMask signing at localhost:8888/link_nado_signer.html).

---

## Overnight Backtest Results (Apr 8, 2026) — DONE

Analyzed 734 trades. Full report: `research/mcp_backtest_results/overnight_analysis.md`

**Key findings:**
1. **BTC is our worst asset**: -$624 across 192 trades. Remove it = +$860 improvement
2. **LONGs crush SHORTs**: LONG +$3,078 vs SHORT -$980. Nado should go LONG-only
3. **Scores are backwards**: Score 2.0-2.5 has 61% WR, Score 4.5-5.0 has 40% WR. Higher ≠ better
4. **TP exits were our best**: 97% WR, +$2,250. We removed them for TREND_FLIP — should add them back
5. **Remove 5 worst symbols** (BTC, DOGE, SOL, SUI, TAO) = +$860 PnL improvement, 363 trades

**Action items (not yet implemented):**
- [ ] Remove/restrict BTC from momentum bots
- [ ] LONG-only mode on Nado
- [ ] Re-add fixed TP at 1.5% alongside TREND_FLIP
- [ ] Test score_min 2.5 with score_max 4.0
- [ ] Wire funding-rates-mcp as live filter

**MCP tools installed** (in `tools/`): funding-rates-mcp, crypto-indicators-mcp. Not yet wired into bots.

**Premium MCPs**: Cerebrus Pulse + Apollo both 404'd. SignalFuse + Coinversaa are Hyperliquid-only (irrelevant).

---

## TODO: Qwen 3.6 Backtest (Apr 6, 2026) — TABLED (rate limits)

**Goal**: Test if Qwen 3.6 (`qwen/qwen3.6-plus:free`) improves Nado/Hibachi trading — better PnL and higher volume.

**Status**: WAITING — free tier will get rate limited. Resume when paid version drops on OpenRouter.

**Model already wired in**: All Qwen references updated to `qwen/qwen3.6-plus:free` in:
- `scripts/momentum_mm.py` (LLM monk mode filter)
- `scripts/grid_mm_nado_v8.py`
- `orchestrator/simplified_decision_engine.py` (Paradex + model map aliases)

**Two tests planned (run in tandem)**:

**Test A — Monk Mode Veto**: Enable existing `--llm-filter` flag. Qwen sees every v9 entry signal, vetoes low-conviction trades. Expected: higher win rate, possibly lower volume.

**Test B — Enhanced Scoring**: New mode where Qwen returns a 1-10 confidence score instead of YES/NO. Use it to:
- Adjust position size (higher confidence = bigger size)
- Lower entry threshold from 3.0 → 2.5 but require Qwen score >= 6 (more volume from marginal setups, filter junk)

**Backtest data**: ~7 days of 15m Binance candles for Nado (20 assets) + Hibachi (6 assets). Existing `scripts/backtest_momentum.py` as base.

**When resuming**: Keep changes clearly marked, don't mess up the repo. Document everything in PROGRESS.md.

---

## DeFi Lending Pro MCP — Intelligence Report (Feb 27, 2026)

**MCP**: `https://defiborrow.loan/mcp` (free, no auth, installed in `~/.claude.json`)

### Key Findings

**Whale Activity:**
- Single wallet `0x1870...a12e` cycling **billions** of USDT in/out of Aave V3 Ethereum (withdrawals: $1.3B, $1.25B, $1.09B, $1.06B, $1B; supplies: $770M, $700M, $400M x6). Looks like treasury/yield management — stablecoin issuer or institutional fund.
- Solana whales dominating: massive flows into Kamino Vaults (kV-AlSOL $250M-$1.5B range), ONYC token deposits ($2.6B, $583M, $472M) — likely points/airdrop farming.
- 45B BONK ($294M) deposited into Marginfi.

**Liquidations: ZERO** — no cascading risk, market healthy, leverage not overextended.

**Flashloan Bots:**
- One bot (`7V3EAR...RPEU`) spamming Marginfi with 30+ rapid 500 WSOL flashloans (~$48k each, 1-sec intervals). Liquidation bot or points farming.
- PRIME token leveraged loop arb via Raydium/Orca on Kamino.

**Best USDC Yields:**
1. Morpho Steakhouse (ETH) — 8.26%
2. Morpho Gauntlet (Base) — 6.60%
3. Morpho Alpha (ETH) — 6.16%
4. Drift (Solana) — 5.70%
5. Morpho MEV Capital (ETH) — 5.60%

**Cheapest USDC Borrow:** Euler on Arbitrum — 0.18% (essentially free)

**ETH Yields:** Topped at 1.89% (Fluid) — low demand for ETH leverage = bearish signal.

**Market Read:** Low-volatility, range-bound. No liquidation cascades to ride. Solana is where all the capital action is. Tight conditions favor TREND_FLIP patience over scalping.

### TODO: Circle Back
- [ ] Wire whale activity alerts into momentum bots as a signal layer (liquidation cascades = momentum)
- [ ] Monitor that `0x1870` USDT whale — if they stop supplying, could signal rate/yield regime change
- [ ] Explore `subscribe_realtime_signals` for Telegram alerts on whale moves
- [ ] Consider Morpho 8.26% USDC vault for idle capital parking
- [ ] Evaluate `find_best_borrow` for leveraged yield strategies once account grows
- [ ] ONYC token on Solana — investigate what this is and why billions are flowing in
