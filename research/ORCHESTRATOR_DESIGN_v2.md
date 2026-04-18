# Orchestrator System Design v2 - Reality-Based

**Date:** 2026-02-03
**Author:** Claude (after user correction)

---

## The Hard Truth

### Current Capital Reality

| Exchange | Balance | Status | Problem |
|----------|---------|--------|---------|
| **Paradex** | ~$100 | VIABLE | Only exchange that can actually trade |
| **Hibachi** | $19.33 | MARGINAL | Can place small orders, barely |
| **Nado** | $12.06 | DEAD | $100 min order + $22 existing position |
| **Extended** | $5.40 | DEAD | Insufficient for any meaningful trade |

**Total: ~$136**
**Usable: ~$120 (Paradex + Hibachi)**

### Capital Decline (Real Data)

| Exchange | Peak | Current | Loss |
|----------|------|---------|------|
| Nado | $62.65 | $12.06 | -80.7% |
| Hibachi | $44.33 | $19.33 | -56.4% |
| Extended | $65.01 | $5.40 | -91.7% |
| Paradex | $92.68 | ~$100 | +7.9% |

**Paradex is the ONLY exchange that hasn't lost money.**

---

## Why We're Losing

### From 16,803 Trades (LEARNINGS.md)

1. **LLM Confidence is Broken**
   - 0.8 confidence = 44.2% actual win rate (expected: 80%)
   - 0.9 confidence = 51.7% actual win rate (expected: 90%)
   - **Implication:** Sizing based on confidence = recipe for losses

2. **Direction Bias**
   - SHORT win rate: 49.4%
   - LONG win rate: 41.8%
   - **Implication:** DEFAULT to SHORT, require higher conviction for LONG

3. **Grid MM Spread Paradox**
   - Tight spreads (1.5 bps) = fills + adverse selection losses
   - Wide spreads (15 bps) = zero fills
   - **Only v8 was profitable** (1.5 bps + ROC pause)

4. **What Actually Works**
   - ROC-based trend detection (pause during moves)
   - POST_ONLY orders (65% fee reduction)
   - Hard exit rules (+2%/-1.5%, 2h min hold)
   - Exchange API P&L tracking (bot calculations are WRONG)

---

## The Realistic Plan

### Phase 1: Capital Consolidation

**Problem:** Can't run 4 agents with $136 split across 4 exchanges.

**Solution:** Consolidate to 2 exchanges:
1. **Paradex** (~$100) - Grid MM primary
2. **Hibachi** ($19) - LLM directional secondary

**Actions:**
- [ ] Close Nado ETH position (currently $22 LONG)
- [ ] Withdraw Nado $12 (blocked - need to solve withdrawal issue)
- [ ] Withdraw Extended $5.40
- [ ] Stop Nado and Extended bots
- [ ] Deposit all to Paradex

**Target State:** Paradex ~$120, Hibachi ~$20

---

### Phase 2: Two-Bot Architecture

Instead of 4 DEX agents + 1 orchestrator, run:

```
┌─────────────────────────────────────────────┐
│            ORCHESTRATOR (simplified)         │
│  - Monitors market regime (ROC, funding)    │
│  - Decides which bot runs: Grid vs LLM      │
│  - Tracks P&L via exchange APIs only        │
└─────────────────────────────────────────────┘
        │                    │
        ▼                    ▼
┌──────────────┐    ┌──────────────────────┐
│  PARADEX BOT │    │     HIBACHI BOT      │
│  Grid MM     │    │   LLM Directional    │
│  BTC-USD     │    │   ETH, SOL (SHORT    │
│  $100 capital│    │   bias preferred)    │
│  POST_ONLY   │    │   $20 capital        │
└──────────────┘    └──────────────────────┘
```

### Phase 3: Orchestrator Logic

**Market Regime Detection:**

```python
def get_market_regime():
    roc = calculate_roc(window=180)  # 3-minute
    funding = get_funding_rate()

    if abs(roc) > 50:
        return "TRENDING"  # Pause grid, allow LLM
    elif abs(roc) < 15:
        return "RANGING"   # Run grid, pause LLM
    else:
        return "MIXED"     # Both can run
```

**Funding Rate Strategy:**

| Funding Rate | Interpretation | Action |
|--------------|----------------|--------|
| > +0.03% | Extreme positive | FAVOR LONGS (squeeze risk) |
| > +0.01% | Positive | Slight long bias |
| -0.01% to +0.01% | Neutral | No bias |
| < -0.01% | Negative | Slight short bias |
| < -0.03% | Extreme negative | FAVOR SHORTS (long liquidations) |

**But remember:** SHORT bias works better anyway (49.4% vs 41.8%), so only override for extreme funding.

---

## Exchange-Specific Edges

### Paradex (PRIMARY - Grid MM)

**Fees:**
- Maker: 0%
- Taker: 0.02%

**Edge:** Zero maker fees + maker rebates on some pairs

**Strategy:**
- Grid MM with POST_ONLY (guaranteed 0% fees)
- BTC-USD-PERP only (most liquid)
- Dynamic spread (v12 calibrated):
  - ROC 0-5 bps → 1.5 bps spread
  - ROC 5-15 bps → 3.0 bps spread
  - ROC 15-30 bps → 6.0 bps spread
  - ROC > 50 bps → PAUSE

**Inventory Management:**
- Max inventory: 100% of capital
- Rebalance at 80%+ one-sided

### Hibachi (SECONDARY - LLM Directional)

**Fees:**
- Maker: 0%
- Taker: 0.035%

**Edge:** Zero maker fees with limit orders + multiple assets

**Strategy:**
- LLM directional with maker_only=True
- SHORT bias preferred (7.6% better WR)
- Trade: ETH, SOL (avoid DOGE - 9% WR, blocked)
- Position size: $10-15 (given $20 balance)

**Entry Rules:**
- Score >= 3.0 (5-signal system)
- Momentum confirmation (HIB-001)
- Only LONG if funding is extreme positive

**Exit Rules:**
- +2% TP / -1.5% SL
- 2h minimum hold
- Trailing stop at +4% (HIB-002)

---

## Swing Trading with Funding Rates

### How Funding Matters

**Perpetual futures charge/pay funding every 8 hours:**
- Positive funding = longs pay shorts
- Negative funding = shorts pay longs

**For SWING trades (multi-hour holds):**

1. **Entry Timing:** Enter before funding payment if on receiving side
2. **Exit Timing:** Exit after receiving funding if holding through
3. **Direction Bias:** Prefer to be on the PAYING side when funding is extreme (contrarian)

**Example:**
- Funding at +0.05% (extreme, longs paying shorts heavily)
- This means the market is overleveraged LONG
- Contrarian: Go LONG (market likely to squeeze upward)
- You'll PAY funding but catch the squeeze

### Funding Rate Trading Rules

```python
def adjust_for_funding(direction, funding_rate):
    """
    Adjust trade direction based on funding rate.
    Returns: (adjusted_direction, confidence_modifier)
    """
    if abs(funding_rate) < 0.01:
        return direction, 1.0  # No adjustment

    if funding_rate > 0.03:  # Extreme positive
        # Market is overleveraged long - squeeze potential
        if direction == "LONG":
            return "LONG", 1.2  # Boost confidence
        else:
            return "SHORT", 0.8  # Reduce confidence

    if funding_rate < -0.03:  # Extreme negative
        # Market is overleveraged short - squeeze potential
        if direction == "SHORT":
            return "SHORT", 1.2  # Boost confidence
        else:
            return "LONG", 0.8  # Reduce confidence

    return direction, 1.0
```

---

## What We're NOT Doing

### Delta-Neutral Arbitrage - WHY NOT

**Theoretical Strategy:**
- Long spot, short perp (or vice versa)
- Collect funding payments
- Market-neutral profit

**Why It's Bad For Us:**
1. **Capital requirement:** Need $500-2000+ to make meaningful returns
2. **We have $136** - spread across 2 legs = $68/leg = tiny funding payments
3. **Funding at 0.01%/8h on $68 = $0.0068** - not worth the complexity
4. **Execution risk:** Two legs to manage, slippage eats profits

**Conclusion:** Not viable at our capital level. Revisit when >$2000.

### High-Frequency Scalping - WHY NOT

**Why It Seems Attractive:**
- Multiple small profits add up
- Less directional exposure

**Why It's Bad For Us:**
1. **Infrastructure:** Need <10ms latency, we're on a home Mac
2. **Edge:** Competing with prop firms with co-located servers
3. **Fees:** Even at 0% maker, we're not fast enough to capture inside spread
4. **Data shows:** Our fill-to-fill P&L is negative (adverse selection)

---

## Implementation Priority

### Immediate (This Week)

1. **Close Nado position** - Free up the $22 stuck in ETH LONG
2. **Solve Nado withdrawal** - Get the $12 out
3. **Withdraw Extended $5.40**
4. **Stop all non-Paradex/Hibachi bots**

### Short-Term (Next 2 Weeks)

5. **Deposit consolidated capital to Paradex**
6. **Run Paradex Grid MM only** (stable, profitable history)
7. **Monitor for 1 week** - validate positive P&L

### Medium-Term (When Capital > $200)

8. **Add Hibachi LLM bot** with SHORT bias
9. **Implement orchestrator** for regime switching
10. **Track funding rate alpha** (does it actually improve WR?)

---

## Success Metrics

### Minimum Viable

- Paradex Grid MM: **Break even or better** over 1 week
- No catastrophic drawdown (>20% in 24h)

### Target

- +5% per week on Grid MM
- Hibachi LLM: 50%+ WR (vs current 40%)
- Combined: +10% per month

### Stretch

- Scale to $500 capital
- Add third exchange when capital allows
- Implement funding rate alpha

---

## Files to Create/Modify

| File | Purpose |
|------|---------|
| `scripts/orchestrator_v1.py` | Main orchestrator logic |
| `scripts/close_nado_position.py` | One-time script to close ETH |
| `PROGRESS.md` | Update with consolidation plan |

---

## Summary

**The Reality:**
- We have $136 total
- Only Paradex is viable for Grid MM
- Hibachi can do small LLM trades
- Nado and Extended are dead

**The Plan:**
1. Consolidate capital
2. Run Paradex Grid MM (proven to work)
3. Add Hibachi LLM when stable
4. Track funding rates but don't over-engineer

**The Rule:**
- NEVER trust bot P&L - query exchange APIs
- DEFAULT to SHORT (7.6% edge)
- POST_ONLY always (65% fee savings)
- Pause during trends (ROC > 50 bps)

---

*This document reflects actual data from the codebase and acknowledges the capital constraints we're working with.*
