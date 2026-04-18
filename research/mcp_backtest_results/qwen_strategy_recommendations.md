Here are the specific, implementable parameters based on your backtest data and capital constraints.

### 1. OPTIMAL STRATEGY CONFIG (Max Volume + Positive PnL)

**Selection:** **Modified Strategy 1 (SHORT Bias Scalper)**.
**Reasoning:** Strategy 1 generates the highest PnL per dollar of volume ($0.155 PnL per $1k Vol) compared to Strategy 3 ($0.09 PnL per $1k Vol). Strategy 3's PnL margin is too thin; after accounting for slippage and potential fee tier changes, it risks bleeding. Strategy 1 has the safety buffer to increase volume.

**Implementation Parameters:**
*   **Direction:** Short Only (for now, see Q3 for adaptation).
*   **Entry Score:** `>= 2.3` (Lowered from 2.5 to increase trade frequency by ~15%).
*   **Indicators:**
    *   MACD: (8, 17, 9)
    *   RSI: (7) — Trigger: Crosses below 65 from above.
    *   Bollinger: (20, 2.5) — Trigger: Price touches/breaks Upper Band.
    *   EMA: (5, 20) — Trigger: 5 crosses below 20.
*   **Exit:**
    *   **TP:** `90 bps` (Lowered from 100 to hit faster, recycling capital for more volume).
    *   **SL:** `55 bps` (Tightened from 60 to protect equity).
    *   **Time Stop:** `90 minutes` (Lowered from 4hr to force churn).
*   **Position Sizing:**
    *   **Nado:** 2 concurrent positions max (due to $100 min notional + 10x lev liquidation risk).
    *   **Hibachi:** 5 concurrent positions max (no min notional, lower lev allows safer distribution).
*   **Order Type:** Limit (Maker) only to capture 0.01% fee rebate/ savings.

### 2. EXCHANGE SPLIT CONFIGURATION

Do not run the same config on both. Diversify logic to smooth equity curve and maximize total volume.

**Exchange A: Nado ($50, 10x Lev, $100 Min)**
*   **Strategy:** **Trend Follow Short** (Modified Strat 1).
*   **Why:** Higher leverage helps hit the $100 min notional with less margin usage, but liquidation risk is higher. Use fewer, higher-conviction trades.
*   **Params:**
    *   Score: `>= 2.5` (Stricter entry).
    *   TP: `100 bps`.
    *   SL: `60 bps`.
    *   Max Positions: `2`.
    *   Size: `100%` of allowed margin per position.

**Exchange B: Hibachi ($50, 5x Lev, No Min)**
*   **Strategy:** **Mean Reversion Scalp** (Modified Strat 3).
*   **Why:** No minimum notional allows you to open tiny positions ($10-$20). This allows high-frequency churning without liquidation risk, driving volume for points while keeping PnL flat/positive.
*   **Params:**
    *   Score: `>= 2.0` (Looser entry for volume).
    *   TP1: `60 bps` (Close 70% of position).
    *   TP2: `150 bps` (Trail remaining 30%).
    *   SL: `45 bps`.
    *   Max Positions: `5`.
    *   Size: `20%` of equity per position (approx $20 notional).

### 3. ADAPTIVE BIAS (LONG/Short Switching)

Hardcoding SHORT is dangerous if the 30-day bearish trend reverses. Implement a **1-Hour Trend Filter** that overrides the 15m strategy signals.

**Logic:**
*   **Indicator:** EMA(200) on **1-Hour** Timeframe.
*   **Condition:**
    *   If `Close_1H < EMA200_1H`: **Enable SHORT signals only.** (Disable Long entries).
    *   If `Close_1H > EMA200_1H`: **Enable LONG signals only.** (Disable Short entries).
    *   *Optional Buffer:* If `Abs(Close - EMA200) < 0.5%`, disable all trading (Chop filter).
*   **Implementation:** Add this as a pre-check gating function before the strategy calculates the entry score.
*   **Alternative ( softer bias):** If `Close < EMA200`, require Entry Score `>= 2.3` for Shorts, but `>= 3.5` for Longs. This maintains Short bias but allows contrarian Longs during strong bounces.

### 4. IMPROVED PARTIAL TP RATIOS

Your current Partial TP (Strat 3) is too slow for high volume. To increase volume, you must close positions faster to free up margin.

**New Config:**
*   **TP1:** `50 bps` (Close **75%** of position).
    *   *Reason:* 50bps is easily reachable in 15m volatility. Closing 75% locks in PnL and frees up 75% of margin immediately for a new trade.
*   **TP2:** `120 bps` (Trail remaining **25%**).
    *   *Reason:* Let the runner catch the fat tail, but don't wait for 300bps.
*   **SL:** `40 bps`.
    *   *Reason:* With 75% closing at 50bps, your breakeven moves quickly. A tighter SL prevents the remaining 25% from dragging the whole trade into a loss.
*   **Breakeven Trigger:** Move SL to Breakeven once TP1 is hit.

### 5. INDICATORS TO BOOST STRATEGY 1 ($0.93/day)

To push the Short strategy PnL higher without sacrificing volume, add these two filters to reduce "noise" trades that hit SL.

**A. Funding Rate Filter**
*   **Logic:** Do not open SHORT if Funding Rate is `< -0.01%` (extremely negative).
*   **Reason:** If funding is deeply negative, the crowd is already short. A short squeeze is likely. Only Short when Funding is `> 0%` or neutral.
*   **Code:** `if funding_rate < -0.0001: return False`

**B. Open Interest (OI) Delta**
*   **Logic:** Require OI to be **decreasing** or **flat** on entry.
*   **Reason:** Price dropping + OI dropping = Long Liquidations (Healthy Short). Price dropping + OI rising = Aggressive Shorting (Risky, potential reversal).
*   **Code:** `if OI_change_15m > 2%: return False` (Avoid entering into parabolic moves).

**C. Time-of-Day Filter (Crucial for Fees)**
*   **Logic:** Pause trading during low liquidity (high slippage eats PnL).
*   **Window:** Disable entries between `00:00 UTC` and `04:00 UTC`.
*   **Reason:** Spreads widen, maker orders get filled less often, and taker slippage increases.

### CRITICAL RISK WARNING (Capital Constraints)

You are trading **$50 accounts with 10x leverage**.
*   **Nado:** $100 min notional. 1 trade = $10 margin. 5 trades = $50 margin (100% utilization).
*   **Liquidation:** If you run 5 positions on Nado at 10x, a **1% move against you** liquidates the entire account.
*   **Recommendation:** Hard-code `Max_Concurrent_Positions = 2` on Nado. This uses 40% of your buying power, giving you a ~2.5% liquidation buffer. On Hibachi, you can run 5 positions because you can size them smaller ($20 notional each) due to no minimum.

**Summary of Immediate Actions:**
1.  Deploy **Modified Strat 1** on Nado (2 pos max, Short bias, 1H EMA filter).
2.  Deploy **Modified Strat 3** on Hibachi (5 pos max, tiny size, fast TP).
3.  Add **Funding Rate** and **1H EMA** filters to all bots immediately.
4.  Monitor **Fee Rebate** status; if your volume tier increases, you can loosen SL slightly to capture more volume.