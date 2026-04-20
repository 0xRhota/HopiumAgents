"""
Momentum Engine - v9-inspired 5-signal scoring system.

Based on the Alpha Arena winning strategy (+22.3% in 17 days):
- 5 signals, each scored 0.0-1.0, summed for 0-5.0 total
- Require score >= 3.0 to trade (high conviction only)
- Momentum confirmation: last candle must agree with direction
- Direction by signal consensus (majority vote)

Signals:
1. RSI(14) — overbought/oversold zones
2. MACD(12,26,9) — crossover and histogram momentum
3. Volume — current vs 20-period average
4. Price Action — distance from recent support/resistance
5. EMA Trend — EMA(8)/EMA(21) crossover and slope
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class MomentumConfig:
    """Strategy parameters."""
    offset_bps: float = 8.0         # Limit offset behind price (18 for Hibachi)
    size_pct: float = 20.0          # Position size as % of leveraged buying power
    leverage: float = 10.0          # Account leverage multiplier (buying power = equity * leverage)
    min_notional: float = 0.0       # Exchange minimum notional (e.g. Nado $100)
    score_min: float = 2.5          # Min score to trade (lowered from 3.0 based on backtest)
    max_positions: int = 2           # Max concurrent positions (2 for Nado, 3 for Hibachi)
    cooldown_seconds: int = 300     # 5 min cooldown after close
    require_volume: bool = False    # Disabled — backtest shows more volume without this gate
    emergency_sl_bps: float = 500.0 # 5% catastrophic stop — safety net only
    # Fixed TP/SL (re-added based on backtest: TP exits were 100% WR)
    tp_bps: float = 80.0           # Take profit (0.8%) — fast recycling
    sl_bps: float = 40.0           # Stop loss (0.4%) — cut losers quick
    max_hold_minutes: float = 120.0 # 2hr max hold — force recycle capital
    # Adaptive ATR-scaled exits (2026-04-20). When enabled, TP/SL scale with
    # asset volatility. High-vol assets get wider exits, low-vol tighter.
    # Floors prevent ultra-tight exits when market is very calm.
    use_atr_exits: bool = False     # Toggle to opt-in per-exchange
    tp_atr_mult: float = 2.0         # TP = max(floor, mult × ATR_bps)
    sl_atr_mult: float = 1.0         # SL = max(floor, mult × ATR_bps)
    tp_bps_floor: float = 30.0       # Don't go below this even if ATR is tiny
    sl_bps_floor: float = 15.0
    atr_period: int = 14             # Classic Wilder ATR lookback


class MomentumEngine:
    """v9-inspired scoring: 5 signals → score 0-5 → trade only >= 3.0."""

    def __init__(self, config: Optional[MomentumConfig] = None):
        self.config = config or MomentumConfig()
        self._last_close_time: float = 0.0

    def detect_trend(self, df: pd.DataFrame) -> dict:
        """
        V9-inspired 5-signal scoring system.

        Each signal scored 0.0-1.0, summed to 0-5.0 total.
        Require score >= score_min (default 3.0) to generate a trade signal.
        Direction determined by majority vote of signal directions.
        Final momentum confirmation: last candle must agree with direction.

        Args:
            df: DataFrame with columns: open, high, low, close, volume
                Must have at least 30 rows.

        Returns:
            {direction, score, strength, rsi, vol_ratio, scoring, ...}
        """
        empty = {
            "direction": "NONE", "score": 0.0, "strength": 0.0,
            "roc_bps": 0.0, "ema_diff_bps": 0.0, "rsi": 50.0,
            "vol_ratio": 1.0, "atr_bps": 0.0, "scoring": "insufficient data",
        }
        if df is None or len(df) < 30:
            return empty

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values

        # ── ATR (Average True Range) in bps ───────────────────────
        # Wilder-style simple avg over atr_period; not EMA (close enough).
        period = max(2, self.config.atr_period)
        if len(close) > period:
            trs = []
            for i in range(1, len(close)):
                tr = max(
                    high[i] - low[i],
                    abs(high[i] - close[i-1]),
                    abs(low[i] - close[i-1]),
                )
                trs.append(tr)
            atr_abs = sum(trs[-period:]) / period
            atr_bps = (atr_abs / close[-1]) * 10000 if close[-1] else 0.0
        else:
            atr_bps = 0.0
        volume = df["volume"].values
        close_s = pd.Series(close)

        # ── Signal 1: RSI(14) ──────────────────────────────────────
        delta = close_s.diff()
        gain = delta.clip(lower=0).rolling(window=14).mean()
        loss = (-delta.clip(upper=0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))
        rsi_val = float(rsi.iloc[-1])

        # v9 thresholds
        if rsi_val < 35 or rsi_val > 65:
            rsi_score = 1.0
        elif rsi_val < 40 or rsi_val > 60:
            rsi_score = 0.7
        elif rsi_val < 45 or rsi_val > 55:
            rsi_score = 0.3
        else:
            rsi_score = 0.0  # 45-55 neutral zone

        # RSI direction: clear zones only (neutral 45-55 doesn't vote)
        if rsi_val < 45:
            rsi_dir = "LONG"
        elif rsi_val > 55:
            rsi_dir = "SHORT"
        else:
            rsi_dir = "NONE"

        # ── Signal 2: MACD(12,26,9) ───────────────────────────────
        ema12 = close_s.ewm(span=12, adjust=False).mean()
        ema26 = close_s.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line

        # Crossover detection
        prev_diff = float(macd_line.iloc[-2] - signal_line.iloc[-2])
        curr_diff = float(macd_line.iloc[-1] - signal_line.iloc[-1])
        crossover = (prev_diff <= 0 and curr_diff > 0) or (prev_diff >= 0 and curr_diff < 0)

        hist_expanding = abs(float(histogram.iloc[-1])) > abs(float(histogram.iloc[-2]))
        hist_strong = abs(float(histogram.iloc[-1])) > abs(float(histogram.iloc[-3]))

        if crossover:
            macd_score = 1.0
        elif hist_expanding and hist_strong:
            macd_score = 0.8
        elif hist_expanding:
            macd_score = 0.5
        else:
            macd_score = 0.0

        macd_dir = "LONG" if curr_diff > 0 else "SHORT"

        # ── Signal 3: Volume ──────────────────────────────────────
        vol_avg = float(volume[-20:].mean()) if len(volume) >= 20 else float(volume.mean())
        vol_ratio = float(volume[-1]) / vol_avg if vol_avg > 0 else 1.0

        # v9 thresholds
        if vol_ratio >= 2.0:
            vol_score = 1.0
        elif vol_ratio >= 1.5:
            vol_score = 0.7
        elif vol_ratio >= 1.2:
            vol_score = 0.4
        else:
            vol_score = 0.0

        # ── Signal 4: Price Action (support/resistance) ──────────
        lookback = min(20, len(high))
        recent_high = float(high[-lookback:].max())
        recent_low = float(low[-lookback:].min())
        price_range = recent_high - recent_low

        if price_range > 0:
            pos_in_range = (close[-1] - recent_low) / price_range

            # At extremes = strong signal (bounce potential)
            if pos_in_range < 0.15 or pos_in_range > 0.85:
                pa_score = 1.0
            elif pos_in_range < 0.25 or pos_in_range > 0.75:
                pa_score = 0.7
            elif pos_in_range < 0.35 or pos_in_range > 0.65:
                pa_score = 0.4
            else:
                pa_score = 0.0  # Mid-range, no edge

            # Near low = LONG bounce, near high = SHORT rejection
            if pos_in_range < 0.35:
                pa_dir = "LONG"
            elif pos_in_range > 0.65:
                pa_dir = "SHORT"
            else:
                pa_dir = "NONE"
        else:
            pa_score = 0.0
            pa_dir = "NONE"

        # ── Signal 5: EMA Trend (8/21 cross + slope) ─────────────
        ema8 = close_s.ewm(span=8, adjust=False).mean()
        ema21 = close_s.ewm(span=21, adjust=False).mean()
        ema_diff_bps = float((ema8.iloc[-1] - ema21.iloc[-1]) / close[-1] * 10000)
        ema_slope_bps = float((ema8.iloc[-1] - ema8.iloc[-3]) / close[-1] * 10000)

        # EMA crossover
        ema_prev = float(ema8.iloc[-2] - ema21.iloc[-2])
        ema_curr = float(ema8.iloc[-1] - ema21.iloc[-1])
        ema_crossover = (ema_prev <= 0 and ema_curr > 0) or (ema_prev >= 0 and ema_curr < 0)

        if ema_crossover:
            ema_score = 1.0
        elif abs(ema_diff_bps) > 20 and abs(ema_slope_bps) > 10:
            ema_score = 0.8
        elif abs(ema_diff_bps) > 10:
            ema_score = 0.5
        else:
            ema_score = 0.0

        ema_dir = "LONG" if ema_diff_bps > 0 else "SHORT"

        # ── Total Score (0-5, matching v9 system) ─────────────────
        score = rsi_score + macd_score + vol_score + pa_score + ema_score

        # ── Direction by v9 confluence (RSI+MACD primary) ─────────
        # v9 logic: LONG needs RSI in buy zone + MACD bullish
        #           SHORT needs RSI in sell zone + MACD bearish
        # Fallback: MACD+EMA trend agreement, or RSI+PA mean-reversion
        candidate = "NONE"
        if rsi_dir != "NONE" and rsi_dir == macd_dir:
            # RSI + MACD agree → strongest v9 signal (entry + momentum)
            candidate = rsi_dir
        elif macd_dir == ema_dir:
            # MACD + EMA agree → clear trend direction
            candidate = macd_dir
        elif rsi_dir != "NONE" and pa_dir != "NONE" and rsi_dir == pa_dir:
            # RSI + PA agree → mean-reversion setup
            candidate = rsi_dir

        direction = "NONE"
        if score >= self.config.score_min and candidate != "NONE":
            # Volume gate: require some volume confirmation if enabled
            if self.config.require_volume and vol_score == 0:
                candidate = "NONE"  # No volume = no trade
            else:
                direction = candidate

        # ── Momentum Confirmation (HIB-001) ───────────────────────
        # Last candle must agree with proposed direction
        if direction != "NONE":
            last_roc_bps = (close[-1] - close[-2]) / close[-2] * 10000
            if direction == "LONG" and last_roc_bps < -5:
                direction = "NONE"  # Price dropping → don't go long
            elif direction == "SHORT" and last_roc_bps > 5:
                direction = "NONE"  # Price rising → don't go short

        # ROC for logging (3-candle)
        roc_bps = (close[-1] - close[-4]) / close[-4] * 10000

        scoring_str = (
            f"RSI={rsi_score:.1f} MACD={macd_score:.1f} "
            f"Vol={vol_score:.1f} PA={pa_score:.1f} EMA={ema_score:.1f}"
        )

        return {
            "direction": direction,
            "score": round(score, 2),
            "strength": round(score / 5.0, 3),  # Normalized 0-1 for compat
            "roc_bps": round(roc_bps, 2),
            "ema_diff_bps": round(ema_diff_bps, 2),
            "rsi": round(rsi_val, 1),
            "vol_ratio": round(vol_ratio, 2),
            "atr_bps": round(atr_bps, 2),
            "scoring": scoring_str,
        }

    def calculate_entry(self, price: float, direction: str) -> float:
        """
        Calculate limit entry price offset behind current price.

        LONG: place limit below price (pullback fill)
        SHORT: place limit above price (pullback fill)
        """
        offset = self.config.offset_bps / 10000.0
        d = self._price_decimals(price)
        if direction == "LONG":
            return round(price * (1 - offset), d)
        elif direction == "SHORT":
            return round(price * (1 + offset), d)
        return price

    @staticmethod
    def _price_decimals(price: float) -> int:
        """Adaptive decimal precision based on price level."""
        if price >= 100:
            return 2
        elif price >= 1:
            return 4
        return 6

    def in_cooldown(self) -> bool:
        """Check if we're still in post-close cooldown."""
        if self._last_close_time == 0:
            return False
        elapsed = time.time() - self._last_close_time
        return elapsed < self.config.cooldown_seconds

    def record_close(self):
        """Mark that a position was just closed (start cooldown)."""
        self._last_close_time = time.time()

    def should_exit(
        self, current_price: float, entry_price: float,
        direction: str, entry_time: float,
        trend: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Exit logic (priority order): TP → SL → TIME → TREND_FLIP → EMERGENCY_SL

        Returns:
            Exit reason string or None
        """
        if entry_price <= 0:
            # Reconciled position — only emergency SL and trend flip
            if trend and trend.get("direction") not in ("NONE", None):
                if trend["direction"] != direction and trend.get("score", 0) >= 2.0:
                    return "TREND_FLIP"
            return None

        # Determine effective TP/SL bps.
        # If use_atr_exits is True AND trend dict carries atr_bps, scale exits
        # to asset volatility. Floors prevent ultra-tight exits in calm markets.
        if self.config.use_atr_exits and trend and trend.get("atr_bps", 0) > 0:
            atr_bps = trend["atr_bps"]
            eff_tp_bps = max(self.config.tp_bps_floor,
                             atr_bps * self.config.tp_atr_mult)
            eff_sl_bps = max(self.config.sl_bps_floor,
                             atr_bps * self.config.sl_atr_mult)
        else:
            eff_tp_bps = self.config.tp_bps
            eff_sl_bps = self.config.sl_bps

        # 1. Take Profit — lock in gains fast
        tp = eff_tp_bps / 10000.0
        if tp > 0:
            if direction == "LONG" and current_price >= entry_price * (1 + tp):
                return "TP"
            if direction == "SHORT" and current_price <= entry_price * (1 - tp):
                return "TP"

        # 2. Stop Loss — cut losers quick
        sl = eff_sl_bps / 10000.0
        if sl > 0:
            if direction == "LONG" and current_price <= entry_price * (1 - sl):
                return "SL"
            if direction == "SHORT" and current_price >= entry_price * (1 + sl):
                return "SL"

        # 3. Time exit — force recycle capital
        hold_minutes = (time.time() - entry_time) / 60.0
        if self.config.max_hold_minutes > 0 and hold_minutes >= self.config.max_hold_minutes:
            return "TIME"

        # 4. Trend flip — market reversed with conviction
        if trend and trend.get("direction") not in ("NONE", None):
            if trend["direction"] != direction and trend.get("score", 0) >= 2.0:
                return "TREND_FLIP"

        # 5. Emergency SL — catastrophic protection (5%)
        esl = self.config.emergency_sl_bps / 10000.0
        if direction == "LONG" and current_price <= entry_price * (1 - esl):
            return "EMERGENCY_SL"
        if direction == "SHORT" and current_price >= entry_price * (1 + esl):
            return "EMERGENCY_SL"

        return None
