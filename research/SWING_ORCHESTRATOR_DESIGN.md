# Swing Trading Orchestrator - Design Document

**Date:** 2026-02-03
**Strategy:** Swing Trading with Funding Rate Exploitation

---

## Strategy Overview

### Core Thesis

Swing trading (multi-hour to multi-day) captures larger moves than scalping while avoiding:
- High-frequency competition (we can't win on latency)
- Adverse selection (Grid MM problem)
- Overtrading (fee drag)

**Funding rates as contrarian signals:**
- Extreme positive funding = market overleveraged LONG → squeeze UP potential
- Extreme negative funding = market overleveraged SHORT → squeeze DOWN potential

### Target Holding Period

| Trade Type | Hold Time | Target P&L | When |
|------------|-----------|------------|------|
| **Swing** | 4h - 48h | +3% to +10% | Default |
| **Scalp** | 15m - 2h | +1% to +2% | High conviction only |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    SWING ORCHESTRATOR                        │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ Funding Rate │  │ Technical    │  │ Cross-DEX    │       │
│  │ Monitor      │  │ Analysis     │  │ Intelligence │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
│         │                │                 │                 │
│         └────────────────┼─────────────────┘                 │
│                          ▼                                   │
│              ┌──────────────────────┐                       │
│              │ SWING DECISION ENGINE │                       │
│              │ - Direction (LONG/SHORT)                     │
│              │ - Conviction (1-5)                            │
│              │ - Best exchange to execute                    │
│              │ - Position size                               │
│              └──────────────────────┘                       │
└─────────────────────────────────────────────────────────────┘
                          │
         ┌────────────────┼────────────────┐
         ▼                ▼                ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   PARADEX    │  │   HIBACHI    │  │    NADO      │
│   EXECUTOR   │  │   EXECUTOR   │  │   EXECUTOR   │
│              │  │              │  │              │
│ Maker: 0%    │  │ Maker: 0%    │  │ POST_ONLY    │
│ Taker: 0.02% │  │ Taker: 0.035%│  │ 100% maker   │
│              │  │              │  │              │
│ BTC, ETH     │  │ All assets   │  │ ETH, BTC,SOL │
└──────────────┘  └──────────────┘  └──────────────┘
```

---

## Component Details

### 1. Funding Rate Monitor

**Data Sources:**
- Binance funding rates (reference)
- Each DEX's native funding (for P&L)

**Signal Generation:**

```python
def get_funding_signal(symbol: str) -> Dict:
    """
    Returns funding-based trading signal.

    Funding Rate Zones:
    - Extreme Positive (>+0.03%): LONG bias (short squeeze potential)
    - Moderate Positive (+0.01% to +0.03%): Slight LONG lean
    - Neutral (-0.01% to +0.01%): No signal
    - Moderate Negative (-0.03% to -0.01%): Slight SHORT lean
    - Extreme Negative (<-0.03%): SHORT bias (long liquidation potential)
    """
    funding_rate = get_current_funding(symbol)

    if funding_rate > 0.03:
        return {
            "signal": "LONG",
            "strength": "STRONG",
            "reasoning": f"Extreme positive funding ({funding_rate:.4%}) - shorts paying heavily, squeeze potential"
        }
    elif funding_rate > 0.01:
        return {
            "signal": "LONG",
            "strength": "WEAK",
            "reasoning": f"Positive funding ({funding_rate:.4%}) - slight LONG lean"
        }
    elif funding_rate < -0.03:
        return {
            "signal": "SHORT",
            "strength": "STRONG",
            "reasoning": f"Extreme negative funding ({funding_rate:.4%}) - longs paying heavily, liquidation potential"
        }
    elif funding_rate < -0.01:
        return {
            "signal": "SHORT",
            "strength": "WEAK",
            "reasoning": f"Negative funding ({funding_rate:.4%}) - slight SHORT lean"
        }
    else:
        return {
            "signal": "NEUTRAL",
            "strength": "NONE",
            "reasoning": f"Neutral funding ({funding_rate:.4%})"
        }
```

**Funding Rate + Hold Time Calculation:**

```python
def calculate_funding_pnl(direction: str, size_usd: float, funding_rate: float, hold_hours: float) -> float:
    """
    Calculate expected funding P&L over hold period.
    Funding settles every 8 hours.
    """
    funding_periods = hold_hours / 8.0
    per_period = size_usd * abs(funding_rate)

    if direction == "LONG":
        # LONG pays when funding positive, receives when negative
        return -per_period * funding_periods if funding_rate > 0 else per_period * funding_periods
    else:
        # SHORT receives when funding positive, pays when negative
        return per_period * funding_periods if funding_rate > 0 else -per_period * funding_periods


# Example:
# LONG $100, funding +0.02%, hold 24h
# funding_periods = 24/8 = 3
# per_period = $100 * 0.0002 = $0.02
# LONG pays: -$0.02 * 3 = -$0.06 funding cost

# SHORT $100, funding +0.02%, hold 24h
# SHORT receives: +$0.02 * 3 = +$0.06 funding income
```

---

### 2. Technical Analysis Engine

**Indicators Used:**
- RSI (14-period) - overbought/oversold
- MACD (12, 26, 9) - trend strength/direction
- OI change (%) - leverage buildup/unwind
- Volume ratio (vs 24h avg) - activity level
- Price vs 20/50 EMA - trend direction

**Signal Scoring (0-5):**

| Signal | Score Contribution |
|--------|-------------------|
| RSI extreme (<30 or >70) | +1 |
| MACD crossover | +1 |
| OI divergence (price up, OI down or vice versa) | +1 |
| Volume spike (>2x average) | +1 |
| Clear trend (price above/below both EMAs) | +1 |

**Score Thresholds:**
- Score < 2.5 → NO_TRADE
- Score 2.5-3.0 → Tier 1 only (BTC, ETH)
- Score 3.0-4.0 → Standard swing
- Score > 4.0 → High conviction (can scalp)

---

### 3. Cross-DEX Intelligence

**What Each Exchange Sees:**

| Exchange | Unique Data | Edge |
|----------|-------------|------|
| **Paradex** | BTC, ETH perps with maker rebates | Lowest cost for BTC/ETH |
| **Hibachi** | Wide asset coverage (SOL, SUI, XRP, DOGE) | More trading pairs |
| **Nado** | ETH, BTC, SOL with zero taker if POST_ONLY | Best for maker fills |
| **Extended** | XCU-USD (unique listing) | New asset alpha |

**Cross-Exchange Signals:**

```python
def detect_cross_exchange_signal() -> Optional[Dict]:
    """
    Detect signals that only appear when comparing exchanges.
    """
    # Example: Price divergence
    paradex_btc = get_paradex_price("BTC")
    hibachi_btc = get_hibachi_price("BTC")

    divergence_pct = (paradex_btc - hibachi_btc) / hibachi_btc * 100

    if abs(divergence_pct) > 0.2:  # 0.2% divergence
        return {
            "type": "PRICE_DIVERGENCE",
            "asset": "BTC",
            "divergence": divergence_pct,
            "action": "BUY" if paradex_btc < hibachi_btc else "SELL",
            "exchange": "paradex" if paradex_btc < hibachi_btc else "hibachi"
        }

    # Example: Funding arbitrage (one exchange has extreme funding)
    paradex_funding = get_paradex_funding("ETH")
    hibachi_funding = get_hibachi_funding("ETH")

    if paradex_funding > 0.02 and hibachi_funding < 0.01:
        return {
            "type": "FUNDING_ARBITRAGE",
            "asset": "ETH",
            "action": "SHORT paradex (receiving), LONG hibachi (paying less)"
        }

    return None
```

---

### 4. Swing Decision Engine

**Decision Flow:**

```
START
  │
  ▼
┌─────────────────────┐
│ Get funding signal  │
└─────────────────────┘
  │
  ▼
┌─────────────────────┐
│ Get technical score │
└─────────────────────┘
  │
  ▼
┌─────────────────────┐
│ Check cross-DEX     │
│ intelligence        │
└─────────────────────┘
  │
  ▼
┌─────────────────────────────────────────┐
│ COMBINE SIGNALS                         │
│                                         │
│ IF funding STRONG + tech score >= 3.0:  │
│   → HIGH CONVICTION SWING               │
│   → Position: 20% of capital            │
│   → Hold target: 24-48h                 │
│   → Exit: +5% TP / -2% SL               │
│                                         │
│ IF funding WEAK + tech score >= 3.0:    │
│   → STANDARD SWING                      │
│   → Position: 10% of capital            │
│   → Hold target: 8-24h                  │
│   → Exit: +3% TP / -1.5% SL             │
│                                         │
│ IF tech score >= 4.0 + momentum conf:   │
│   → SCALP ALLOWED                       │
│   → Position: 15% of capital            │
│   → Hold target: 15m-2h                 │
│   → Exit: +1.5% TP / -1% SL             │
│                                         │
│ ELSE: NO_TRADE                          │
└─────────────────────────────────────────┘
  │
  ▼
┌─────────────────────┐
│ Select best exchange│
│ for execution       │
└─────────────────────┘
  │
  ▼
┌─────────────────────┐
│ Execute via agent   │
└─────────────────────┘
```

---

### 5. Exchange Selection Logic

**When to use each exchange:**

```python
def select_exchange(asset: str, direction: str, size_usd: float) -> str:
    """
    Select optimal exchange for execution.
    """
    # Asset availability
    if asset == "XCU":
        return "extended"  # Only exchange with XCU

    if asset in ["SUI", "XRP", "DOGE"]:
        return "hibachi"  # Widest asset coverage

    # For BTC, ETH, SOL - compare fees and liquidity

    # If size > $50 and we want maker fills
    if size_usd > 50:
        # Paradex has best BTC liquidity
        if asset == "BTC":
            return "paradex"
        # Nado has good ETH liquidity with POST_ONLY
        if asset == "ETH":
            return "nado"

    # Default to lowest fees
    return "paradex"
```

---

## Position Management

### Entry Rules

1. **Swing Entry (Default):**
   - Use limit orders (maker fees)
   - Place 0.5% inside best bid/ask
   - Wait up to 60 seconds for fill
   - Cancel and retry at market if not filled

2. **Scalp Entry (High Conviction Only):**
   - Use aggressive limit (inside spread)
   - 10-second timeout then market

### Exit Rules

| Trade Type | Take Profit | Stop Loss | Time Stop |
|------------|-------------|-----------|-----------|
| High Conviction Swing | +5% | -2% | 48h |
| Standard Swing | +3% | -1.5% | 24h |
| Scalp | +1.5% | -1% | 2h |

### Trailing Stop (When Profitable)

```python
def check_trailing_stop(position: Dict) -> bool:
    """
    Trailing stop logic for winning trades.
    """
    pnl_pct = position['unrealized_pnl_pct']

    # Breakeven at +4%
    if pnl_pct >= 4.0:
        position['stop_loss'] = max(position['stop_loss'], 0.0)

    # Trail at +6%
    if pnl_pct >= 6.0:
        new_stop = pnl_pct - 2.0  # 2% trailing distance
        position['stop_loss'] = max(position['stop_loss'], new_stop)

    return position['unrealized_pnl_pct'] <= position['stop_loss']
```

---

## Short Bias Implementation

**From data: SHORT has 49.4% WR vs LONG 41.8% (7.6% edge)**

```python
def apply_short_bias(signal: Dict) -> Dict:
    """
    Apply SHORT bias to decision making.
    """
    direction = signal['direction']
    score = signal['score']

    if direction == "SHORT":
        # SHORT gets a boost
        signal['adjusted_score'] = score + 0.5
        signal['bias_note'] = "SHORT bias applied (+0.5)"
    elif direction == "LONG":
        # LONG needs stronger conviction
        # Only boost if funding is extreme positive (squeeze potential)
        if signal.get('funding_strength') == "STRONG":
            signal['adjusted_score'] = score
            signal['bias_note'] = "LONG allowed: extreme funding override"
        else:
            signal['adjusted_score'] = score - 0.5
            signal['bias_note'] = "LONG penalty applied (-0.5)"

    return signal
```

---

## Fee Optimization

### Exchange Fee Comparison

| Exchange | Maker | Taker | Best For |
|----------|-------|-------|----------|
| Paradex | 0% | 0.02% | BTC swings (most liquid) |
| Hibachi | 0% | 0.035% | Alt-coin swings |
| Nado | 0% (POST_ONLY) | 0.035% | ETH maker-only |
| Extended | 0% | 0.025% | XCU trades |

### Fee-Aware Sizing

```python
def calculate_breakeven_move(entry_price: float, exit_fee_rate: float, entry_fee_rate: float = 0) -> float:
    """
    Calculate minimum price move needed to break even after fees.
    """
    total_fee_rate = entry_fee_rate + exit_fee_rate
    breakeven_pct = total_fee_rate * 100

    # If we use maker on entry, taker on exit:
    # breakeven = 0% + 0.02% = 0.02%

    # If we use taker on both:
    # breakeven = 0.02% + 0.02% = 0.04%

    return breakeven_pct
```

**Implication:** With 0% maker entry + 0.02% taker exit, need only 0.02% move to break even. For a +3% target, fees are <1% of profit.

---

## Monitoring & P&L Tracking

### CRITICAL: Exchange API Only

```python
async def get_real_pnl(exchange: str) -> Dict:
    """
    Get P&L from exchange API ONLY.
    NEVER trust bot-calculated P&L.
    """
    if exchange == "nado":
        # Nado Archive API
        matches = await nado_sdk.get_pnl(hours=24)
        return {
            "realized_pnl": matches['realized_pnl'],
            "fees": matches['fees'],
            "net_pnl": matches['net_pnl']
        }
    elif exchange == "paradex":
        # Paradex account endpoint
        account = await paradex_client.account.get()
        return {
            "equity": account.account_value,
            "unrealized_pnl": account.unrealized_pnl
        }
    # ... etc
```

### Daily Report

```
=== SWING ORCHESTRATOR DAILY REPORT ===
Date: 2026-02-03

TRADES:
  - ETH SHORT @ $3,200 → Closed $3,150 (+1.56%) via Nado
  - BTC LONG @ $97,000 → Open (+0.8%) via Paradex

FUNDING COLLECTED:
  - ETH SHORT: +$0.12 (3 funding periods)
  - BTC LONG: -$0.08 (2 funding periods)
  - Net Funding: +$0.04

P&L SUMMARY (Exchange API):
  - Paradex: +$2.50
  - Nado: +$1.80
  - Hibachi: -$0.50
  - Total: +$3.80

WIN RATE (7-day):
  - Swings: 4/6 (66%)
  - Scalps: 1/2 (50%)
```

---

## Implementation Phases

### Phase 1: Core Orchestrator
- [ ] Funding rate monitor (Binance API)
- [ ] Technical score calculator (reuse existing)
- [ ] Basic swing decision engine
- [ ] Single exchange execution (Paradex)

### Phase 2: Multi-Exchange
- [ ] Add Hibachi executor
- [ ] Add Nado executor
- [ ] Exchange selection logic
- [ ] Cross-exchange price comparison

### Phase 3: Intelligence
- [ ] Funding arbitrage detection
- [ ] OI divergence signals
- [ ] Cross-exchange divergence
- [ ] Historical pattern matching

### Phase 4: Refinement
- [ ] Tune thresholds based on live data
- [ ] Add/remove signals based on performance
- [ ] Optimize exchange routing
- [ ] Scale capital allocation

---

## Files to Create

| File | Purpose |
|------|---------|
| `orchestrator/swing_orchestrator.py` | Main orchestrator |
| `orchestrator/funding_monitor.py` | Funding rate tracking |
| `orchestrator/technical_engine.py` | Score calculation |
| `orchestrator/exchange_router.py` | Exchange selection |
| `orchestrator/position_manager.py` | Entry/exit management |

---

## Summary

**What This Strategy Does:**
1. Monitors funding rates for contrarian squeeze signals
2. Combines with technical score for entry timing
3. Routes to best exchange for execution
4. Holds for hours/days (not minutes)
5. Uses funding rate income as bonus P&L
6. Defaults to SHORT bias (7.6% historical edge)

**What This Strategy Does NOT Do:**
- Grid market making
- High-frequency scalping
- Delta-neutral arbitrage
- Confidence-based position sizing

**Success Criteria:**
- Average hold time: 8-24 hours
- Win rate target: 55%+
- Average winner: +3%
- Average loser: -1.5%
- Expected value per trade: +0.9% (55% × 3% - 45% × 1.5%)
