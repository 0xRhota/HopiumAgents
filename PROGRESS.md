# Hopium Agents - Progress Tracker

**Last Updated:** 2026-01-30

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

## Exchange Accounts (2026-01-22)

| Exchange | Balance | Bot Running | Recent P&L |
|----------|---------|-------------|------------|
| Paradex | $92.68 | Grid MM v12 (BTC) | -$0.01 (24h) |
| Nado | $62.65 | Grid MM v18 (ETH) | +$0.07 (24h) |
| Hibachi | $44.33 | Grid MM (BTC) + LLM (ETH/SOL/SUI/XRP) | N/A |
| Extended | $65.01 | Grid MM v18 POST_ONLY (BTC) | - (just started) |

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

## TODO: Nado Withdrawal Issue (2026-01-21)

**Problem**: Cannot withdraw from Nado - signature mismatch error.

**Error**: "The provided signature does not match with the sender's or the linked signer's. Signer: 0x36e82b224b3bd44884d6ccb8c54e5ee85131e1e6"

**Context**: 
- Linked signer (0x36e82b...) was set up for bot trading
- Linked signers can trade but apparently cannot withdraw
- User's main wallet is MetaMask, same one used to set up Nado account
- Withdrawal should work with main wallet signature, but it's not

**To Investigate**:
1. Why is Nado trying to use linked signer for withdrawal instead of main wallet?
2. Is there a way to withdraw using the main wallet directly?
3. May need to contact Nado support or check their docs on linked signer permissions

**Blocked**: Need native ETH on INK chain to pay gas for any transactions

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

## Current Bot Status (2026-01-30)

| Bot | Asset | Strategy | Status |
|-----|-------|----------|--------|
| Hibachi LLM Bot | ETH, SOL, SUI, XRP, DOGE | Strategy F (MAKER-ONLY) | ✅ RUNNING |
| Hibachi Grid MM | BTC | Spread capture | ✅ RUNNING |
| Nado Grid MM + LLM | ETH (grid) + BTC/SOL (LLM) | v19 Grid + LLM directional | ✅ RUNNING |
| Paradex Grid MM | BTC | Spread capture | ✅ RUNNING |
| Extended LLM | BTC, ETH, SOL, XCU | Strategy D pairs trade | ✅ RUNNING |
| Extended Grid MM | BTC | v18 POST_ONLY (0% maker fee) | ✅ RUNNING |

**Real Exchange Data (2026-01-30 23:00):**

| Exchange | Balance | Fills (session) | Notes |
|----------|---------|-----------------|-------|
| Hibachi | $25.25 | - | LLM restarted after sleep |
| Nado | $33.93 | 1,046 (14.7/hr) | ETH LONG 0.024 |
| Paradex | $83.11 | - | Grid running |
| Extended | $21.87 | 1,750 (11.8/hr) | XCU-USD added to whitelist |
| **Total** | **~$164** | | |

**P&L Tracker:** `python3 scripts/pnl_tracker.py` (queries real exchange APIs)

**Commands:**
```bash
# Grid MM (BTC)
nohup python3 -u scripts/grid_mm_hibachi.py > logs/grid_mm_hibachi.log 2>&1 &

# LLM Directional (all except BTC) - MAKER-ONLY
nohup python3 -u -m hibachi_agent.bot_hibachi --live --strategy F --interval 600 > logs/hibachi_bot.log 2>&1 &
```
