# Fix ROC Window for Dynamic Spread Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix the dynamic spread system by extending the ROC calculation window from 10 seconds to 3 minutes, allowing spread thresholds (5/15/30/50 bps) to actually trigger during market trends.

**Architecture:** The current ROC uses `prices[-10]` with 1-second samples = 10-second window. This never exceeds 5 bps even in real trends, so spread stays at 1.5 bps. Fix by: (1) increase `price_history` maxlen to 360 (6 min buffer), (2) change ROC lookback to `prices[-180]` (3 min window).

**Tech Stack:** Python, asyncio, collections.deque

---

## Background

Current behavior:
- `price_history: deque = deque(maxlen=30)` - only 30 seconds of history
- `past = prices[-10]` - ROC over 10 seconds
- Result: ROC rarely exceeds 5 bps, spread stuck at 1.5-3.0 bps

Expected behavior after fix:
- `price_history: deque = deque(maxlen=360)` - 6 minutes of history
- `past = prices[-180]` - ROC over 3 minutes
- Result: ROC will hit 15-30 bps during moderate trends, triggering wider spreads

---

### Task 1: Update Nado Grid Bot ROC Window

**Files:**
- Modify: `scripts/grid_mm_nado_v8.py:91` (price_history maxlen)
- Modify: `scripts/grid_mm_nado_v8.py:320-324` (_calculate_roc method)

**Step 1: Update price_history buffer size**

In `scripts/grid_mm_nado_v8.py`, find line 91:
```python
self.price_history: deque = deque(maxlen=30)
```

Change to:
```python
self.price_history: deque = deque(maxlen=360)  # 6 minutes at 1/sec
```

**Step 2: Update ROC calculation window**

In `scripts/grid_mm_nado_v8.py`, find the `_calculate_roc` method around lines 318-327:
```python
def _calculate_roc(self) -> float:
    """Calculate Rate of Change in bps"""
    if len(self.price_history) < 10:
        return 0.0
    prices = list(self.price_history)
    current = prices[-1]
    past = prices[-10]
    if past == 0:
        return 0.0
    return (current - past) / past * 10000
```

Change to:
```python
def _calculate_roc(self) -> float:
    """Calculate Rate of Change in bps over 3-minute window"""
    if len(self.price_history) < 180:
        return 0.0  # Need 3 min of data before calculating ROC
    prices = list(self.price_history)
    current = prices[-1]
    past = prices[-180]  # 3 minutes ago (180 samples at 1/sec)
    if past == 0:
        return 0.0
    return (current - past) / past * 10000
```

**Step 3: Verify changes**

Run: `grep -n "maxlen=360\|prices\[-180\]" scripts/grid_mm_nado_v8.py`
Expected output should show both changes.

---

### Task 2: Update Paradex Grid Bot ROC Window

**Files:**
- Modify: `scripts/grid_mm_live.py:82` (price_history maxlen)
- Modify: `scripts/grid_mm_live.py:282-292` (_calculate_roc method)

**Step 1: Update price_history buffer size**

In `scripts/grid_mm_live.py`, find line 82:
```python
self.price_history: deque = deque(maxlen=30)
```

Change to:
```python
self.price_history: deque = deque(maxlen=360)  # 6 minutes at 1/sec
```

**Step 2: Update ROC calculation window**

In `scripts/grid_mm_live.py`, find the `_calculate_roc` method around lines 282-292:
```python
def _calculate_roc(self) -> float:
    """Calculate Rate of Change in bps"""
    if len(self.price_history) < 10:
        return 0.0

    prices = list(self.price_history)
    current = prices[-1]
    past = prices[-10]

    if past == 0:
        return 0.0
    return (current - past) / past * 10000
```

Change to:
```python
def _calculate_roc(self) -> float:
    """Calculate Rate of Change in bps over 3-minute window"""
    if len(self.price_history) < 180:
        return 0.0  # Need 3 min of data before calculating ROC

    prices = list(self.price_history)
    current = prices[-1]
    past = prices[-180]  # 3 minutes ago (180 samples at 1/sec)

    if past == 0:
        return 0.0
    return (current - past) / past * 10000
```

**Step 3: Verify changes**

Run: `grep -n "maxlen=360\|prices\[-180\]" scripts/grid_mm_live.py`
Expected output should show both changes.

---

### Task 3: Update Tests

**Files:**
- Modify: `tests/test_dynamic_spread.py` (add ROC window tests)

**Step 1: Add test for ROC calculation with new window**

Add these tests to `tests/test_dynamic_spread.py`:

```python
class TestROCCalculation:
    """Test ROC calculation with 3-minute window"""

    def test_roc_needs_180_samples(self):
        """ROC returns 0 if less than 180 samples"""
        from collections import deque
        price_history = deque(maxlen=360)

        # Add only 100 samples
        for i in range(100):
            price_history.append(3000.0)

        # Simulate _calculate_roc logic
        if len(price_history) < 180:
            roc = 0.0
        else:
            prices = list(price_history)
            roc = (prices[-1] - prices[-180]) / prices[-180] * 10000

        assert roc == 0.0, "Should return 0 with insufficient samples"

    def test_roc_3min_window_detects_trend(self):
        """ROC should detect 0.5% move over 3 minutes as ~50 bps"""
        from collections import deque
        price_history = deque(maxlen=360)

        # Simulate 0.5% drop over 3 minutes (180 samples)
        start_price = 3000.0
        end_price = 2985.0  # 0.5% lower

        for i in range(180):
            # Linear interpolation
            price = start_price - (start_price - end_price) * (i / 179)
            price_history.append(price)

        prices = list(price_history)
        roc = (prices[-1] - prices[-180]) / prices[-180] * 10000

        # Should be approximately -50 bps
        assert -55 < roc < -45, f"Expected ~-50 bps, got {roc}"
```

**Step 2: Run tests**

Run: `pytest tests/test_dynamic_spread.py -v`
Expected: All tests pass

---

### Task 4: Restart Bots with Fix

**Step 1: Stop running bots**

```bash
pkill -f grid_mm_nado && pkill -f grid_mm_live
sleep 2
ps aux | grep grid_mm | grep -v grep || echo "All grid bots stopped"
```

**Step 2: Start Nado bot with logs**

```bash
cd /Users/admin/Documents/Projects/pacifica-trading-bot
nohup python3 scripts/grid_mm_nado_v8.py > logs/grid_mm_nado.log 2>&1 &
sleep 3
tail -20 logs/grid_mm_nado.log
```

Expected: Bot starts, shows "Initial price" and waits for 3 min to build price history.

**Step 3: Start Paradex bot with logs**

```bash
nohup python3.11 scripts/grid_mm_live.py > logs/grid_mm_live.log 2>&1 &
sleep 3
tail -20 logs/grid_mm_live.log
```

Expected: Bot starts, shows "Account balance" and waits for 3 min to build price history.

---

### Task 5: Update Documentation

**Files:**
- Modify: `LEARNINGS.md` (add ROC window fix lesson)
- Modify: `PROGRESS.md` (update bot configs)

**Step 1: Add to LEARNINGS.md**

Add new section after the POST_ONLY section:

```markdown
### CRITICAL: ROC Window Must Match Spread Thresholds (2026-01-19)

**Problem**: Dynamic spread (v12) was stuck at 1.5-3.0 bps even during real trends. Nado lost $17 in one day despite 100% maker rate.

**Root Cause**: ROC window mismatch
- Spread thresholds designed for: 5/15/30/50 bps
- Actual ROC window: 10 seconds (rarely exceeds 5 bps)
- Result: Spread never widens, bot gets run over by slow trends

**Evidence** (from logs):
```
14:55:26 | SPREAD WIDENED: 1.5 → 3.0 bps (ROC: +5.2)
14:55:30 | SPREAD TIGHTENED: 3.0 → 1.5 bps (ROC: +4.0)  # 4 seconds later!
```
Spread flip-flopped every few seconds, never staying wide.

**Fix Applied**:
- `price_history` maxlen: 30 → 360 (6 minutes buffer)
- ROC lookback: `prices[-10]` → `prices[-180]` (3-minute window)

**Expected Impact**:
| Move Speed | Old ROC | New ROC | Spread |
|------------|---------|---------|--------|
| 0.5%/30min | 0.3 bps | 5 bps | 3.0 bps |
| 0.5%/10min | 0.8 bps | 15 bps | 6.0 bps |
| 0.3%/5min | 1.0 bps | 18 bps | 6.0 bps |

**Files Changed**:
- `scripts/grid_mm_nado_v8.py` - ROC window fix
- `scripts/grid_mm_live.py` - ROC window fix
```

**Step 2: Update PROGRESS.md**

Update the Grid MM configuration sections to note the 3-minute ROC window.

---

## Verification

After all tasks complete, monitor logs for 5-10 minutes:

```bash
tail -f logs/grid_mm_nado.log | grep -E "ROC:|SPREAD"
```

Expected behavior:
- First 3 minutes: ROC shows 0.0 (building history)
- After 3 minutes: ROC shows actual values
- During price moves: Spread should widen and STAY wide, not flip-flop

---

## Rollback

If issues occur, revert to 10-second window:
1. Change `maxlen=360` back to `maxlen=30`
2. Change `prices[-180]` back to `prices[-10]`
3. Change `< 180` check back to `< 10`
4. Restart bots
