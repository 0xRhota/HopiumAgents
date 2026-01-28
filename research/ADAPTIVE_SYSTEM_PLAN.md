# Adaptive Trading System Plan

**Date:** 2026-01-10
**Status:** Ready for Implementation
**Consultation:** 3 rounds with Qwen-72B

---

## Executive Summary

This plan addresses strategy degradation through a 3-component adaptive system:
1. **Real-time Regime Detection** - Adapt to market conditions
2. **Confidence Calibration** - Fix the 0.8 confidence = 44% WR trap
3. **Circuit Breakers** - Prevent catastrophic loss sequences

**MVP delivers 80% of value** with regime detection + calibration + parameter adaptation.

---

## Problem Statement (Validated)

### Degradation Evidence Found in Codebase:

| Issue | Location | Status |
|-------|----------|--------|
| Confidence trap (0.8 conf = 44% WR) | `llm_agent/self_learning.py:196-239` | Code EXISTS but UNUSED |
| `get_adjusted_confidence()` | `llm_agent/shared_learning.py:412-441` | NEVER CALLED |
| Hardcoded stop-loss (2.0%) | `hibachi_agent/execution/hard_exit_rules.py:30` | Static |
| Regime cache (1 hour) | `llm_agent/data/market_context.py` | Too slow for crypto |
| No circuit breakers | All trading loops | Missing entirely |

---

## Solution Architecture

### Component 1: Regime Detection

**Cadence:** Every 5 minutes
**Lookback:** 15 minutes of 1m candles
**Method:** Multi-indicator classification

#### Regime States:
```
TRENDING_UP    - ADX > 25, price > SMA20, positive momentum
TRENDING_DOWN  - ADX > 25, price < SMA20, negative momentum
CHOPPY         - ADX < 20, high ATR relative to range
RANGE_BOUND    - ADX < 20, price within Bollinger Bands, low ATR
```

#### Regime-Specific Parameters:

| Regime | Stop-Loss Multiplier | Max Hold | Position Size |
|--------|---------------------|----------|---------------|
| TRENDING_UP | 1.5x base | 24h | 100% |
| TRENDING_DOWN | 1.5x base | 24h | 100% |
| CHOPPY | 2.0x base | 12h | 70% |
| RANGE_BOUND | 2.5x base | 6h | 50% |

Base stop-loss = 2.0%, so:
- Trending: 3.0% stop
- Choppy: 4.0% stop
- Range-bound: 5.0% stop

---

### Component 2: Confidence Calibration

**Method:** Platt scaling
**Recomputation:** Every 24 hours (rolling window)
**Minimum samples:** 100 trades before symbol-specific calibration

#### Implementation:
```python
def calibrate_confidence(raw_confidence, calibration_params):
    """
    Platt scaling: P(y=1|f) = 1 / (1 + exp(A*f + B))
    where f = raw_confidence, A and B are fitted parameters
    """
    A, B = calibration_params['A'], calibration_params['B']
    return 1 / (1 + np.exp(A * raw_confidence + B))
```

#### Current Calibration Table (from 16,803 trades):
| Raw Confidence | Actual WR | Calibrated Confidence |
|----------------|-----------|----------------------|
| 0.6 | 46.2% | 0.46 |
| 0.7 | 44.7% | 0.45 |
| 0.8 | 44.2% | 0.44 |
| 0.9 | 51.7% | 0.52 |

**Key insight:** Don't size up on high raw confidence. Use calibrated values.

---

### Component 3: Circuit Breakers

#### Triggers:
1. **Consecutive losses:** 5 losses in a row (regardless of wins between)
2. **Daily drawdown:** 5% rolling 24h drawdown

#### Cooldown:
- **Duration:** 1 hour minimum
- **Override:** One trade at 50% size allowed during cooldown if regime shows high conviction
- **Extended cooldown:** If override trade loses, extend cooldown by 1 hour

#### Implementation:
```python
class CircuitBreaker:
    def __init__(self):
        self.loss_count = 0
        self.cooldown_until = None
        self.daily_pnl = []

    def record_trade(self, pnl):
        if pnl < 0:
            self.loss_count += 1
        else:
            self.loss_count = 0  # Reset on win

        self.daily_pnl.append({'time': time.time(), 'pnl': pnl})
        self._cleanup_old_pnl()

    def is_triggered(self):
        # Check consecutive losses
        if self.loss_count >= 5:
            return True

        # Check rolling 24h drawdown
        if self._calculate_daily_drawdown() >= 0.05:
            return True

        return False
```

---

## Edge Cases (Finalized)

### 1. Regime Transition Mid-Trade
- **Decision:** Honor original parameters for open positions
- **New entries:** Use new regime parameters
- **Rationale:** Prevents whipsaw exits from regime flicker

### 2. Calibration Cold Start
- **Fallback:** Global calibration (all symbols pooled)
- **Sizing:** 50% of normal until 100 symbol-specific trades
- **Rationale:** Conservative until data proves otherwise

### 3. Conflicting Signals (High confidence + CHOPPY regime)
- **Decision:** Allow trade but reduce size to 50%
- **Rationale:** Don't veto high-conviction signals, but manage risk

### 4. High-Conviction During Cooldown
- **Decision:** Allow ONE trade at 50% size
- **Penalty:** If loses, extend cooldown by 1 hour
- **Rationale:** Don't miss obvious opportunities but remain cautious

### 5. Data Quality Issues
- **Missing candles:** >3 consecutive = pause trading for symbol
- **Stale data:** >5 minutes old = regime detection invalid
- **Rationale:** Bad data = bad decisions

---

## Module Structure

```
llm_agent/
├── adaptive/
│   ├── __init__.py
│   ├── regime_detector.py      # RegimeDetector class (per-symbol)
│   ├── confidence_calibrator.py # ConfidenceCalibrator class
│   ├── circuit_breaker.py       # CircuitBreaker class
│   └── adaptive_manager.py      # Orchestrates all three
├── data/
│   └── calibration/             # JSON files per symbol
│       ├── BTC.json
│       ├── ETH.json
│       └── global.json          # Fallback calibration
```

### Integration Points:

```python
# In main trading loop (e.g., bot_hibachi.py)

from llm_agent.adaptive import AdaptiveManager

adaptive = AdaptiveManager(symbol='ETH')

# Before making trade decision
if adaptive.circuit_breaker.is_triggered():
    if not adaptive.should_override_cooldown(regime_conviction):
        continue  # Skip this cycle

# Get regime-adjusted parameters
params = adaptive.get_trade_parameters()
# Returns: {'stop_loss': 0.04, 'max_hold_hours': 12, 'size_multiplier': 0.7}

# After LLM decision
calibrated_confidence = adaptive.calibrate(raw_confidence)
position_size = base_size * calibrated_confidence * params['size_multiplier']

# After trade closes
adaptive.record_trade_result(pnl)
```

---

## Implementation Roadmap

### Phase 1: MVP (Captures 80% of Value)

**Week 1:**
1. Create `llm_agent/adaptive/` directory structure
2. Implement `RegimeDetector` with 5-min cadence
3. Implement `ConfidenceCalibrator` with Platt scaling
4. Basic `CircuitBreaker` (5 consecutive losses, 5% drawdown)

**Week 2:**
5. Create `AdaptiveManager` to orchestrate components
6. Integrate into ONE trading bot (start with Hibachi)
7. Persist calibration to JSON files
8. Add logging for all adaptive decisions

### Phase 2: Refinement (Deferred)

- Advanced regime detection (HMM-based)
- Per-symbol calibration (needs more trade data per symbol)
- Backtesting framework for adaptive parameters
- Dashboard for monitoring regime states

---

## Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| Win rate (overall) | ~44% | 48%+ |
| Confidence calibration error | 35.8% gap | <10% gap |
| Max consecutive losses | Unlimited | 5 max |
| Drawdown before pause | None | 5% |
| Strategy adaptiveness | Static | 4 regime states |

---

## Files to Create/Modify

### New Files:
- `llm_agent/adaptive/__init__.py`
- `llm_agent/adaptive/regime_detector.py`
- `llm_agent/adaptive/confidence_calibrator.py`
- `llm_agent/adaptive/circuit_breaker.py`
- `llm_agent/adaptive/adaptive_manager.py`
- `llm_agent/data/calibration/global.json`

### Modified Files:
- `hibachi_agent/bot_hibachi.py` - Add adaptive manager integration
- `llm_agent/llm/llm_client.py` - Use calibrated confidence in responses
- `hibachi_agent/execution/hard_exit_rules.py` - Use regime-based parameters

---

## Risk Considerations

1. **Over-optimization risk:** Parameters derived from historical data may not generalize
   - Mitigation: Use conservative defaults, gradual rollout

2. **Regime misclassification:** Wrong regime = wrong parameters
   - Mitigation: Log all regime decisions, monitor classification accuracy

3. **Calibration drift:** Market dynamics change calibration
   - Mitigation: 24h rolling recalibration, alert on significant drift

4. **Implementation bugs:** New system could introduce errors
   - Mitigation: Paper trade new system parallel to live before switching

---

## Appendix: Research Sources

1. **QuantInsti:** "Market Regime using Hidden Markov Model" - HMM-based regime detection with walk-forward training
2. **PyQuantLab:** "Regime Filtered Trend Strategy" - ADX/ATR/Bollinger for regime classification
3. **Neptune.ai:** "Brier Score and Model Calibration" - Platt scaling implementation details

---

*Generated through iterative consultation with Qwen-72B (3 rounds)*
