"""
Regime Detector - Classifies market conditions

Regimes:
- TRENDING_UP: ADX > 25, price > SMA20, positive momentum
- TRENDING_DOWN: ADX > 25, price < SMA20, negative momentum
- CHOPPY: ADX < 20, high ATR relative to range
- RANGE_BOUND: ADX < 20, price within Bollinger Bands, low ATR

Cadence: Every 5 minutes
Lookback: 15 minutes of 1m candles (or 15 bars of 15m candles)
"""

import logging
from enum import Enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    """Market regime classification"""
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    CHOPPY = "CHOPPY"
    RANGE_BOUND = "RANGE_BOUND"
    UNKNOWN = "UNKNOWN"


@dataclass
class RegimeParameters:
    """Trading parameters for each regime"""
    stop_loss_multiplier: float  # Multiplier on base stop loss
    max_hold_hours: float        # Maximum hold duration
    size_multiplier: float       # Position size multiplier


# Regime-specific parameters from Qwen consultation
REGIME_PARAMS = {
    MarketRegime.TRENDING_UP: RegimeParameters(
        stop_loss_multiplier=1.5,  # 3.0% stop (1.5 * 2.0% base)
        max_hold_hours=24.0,
        size_multiplier=1.0
    ),
    MarketRegime.TRENDING_DOWN: RegimeParameters(
        stop_loss_multiplier=1.5,
        max_hold_hours=24.0,
        size_multiplier=1.0
    ),
    MarketRegime.CHOPPY: RegimeParameters(
        stop_loss_multiplier=2.0,  # 4.0% stop
        max_hold_hours=12.0,
        size_multiplier=0.7
    ),
    MarketRegime.RANGE_BOUND: RegimeParameters(
        stop_loss_multiplier=2.5,  # 5.0% stop
        max_hold_hours=6.0,
        size_multiplier=0.5
    ),
    MarketRegime.UNKNOWN: RegimeParameters(
        stop_loss_multiplier=2.0,
        max_hold_hours=12.0,
        size_multiplier=0.5  # Conservative when unknown
    ),
}


class RegimeDetector:
    """
    Detects market regime based on technical indicators.

    Uses ADX, ATR, Bollinger Bands, and SMA for classification.
    """

    def __init__(
        self,
        symbol: str,
        adx_trend_threshold: float = 25.0,
        adx_range_threshold: float = 20.0,
        cache_duration_seconds: int = 300,  # 5 minutes
    ):
        """
        Initialize regime detector.

        Args:
            symbol: Trading symbol
            adx_trend_threshold: ADX value above which market is trending
            adx_range_threshold: ADX value below which market is ranging
            cache_duration_seconds: How long to cache regime detection
        """
        self.symbol = symbol
        self.adx_trend_threshold = adx_trend_threshold
        self.adx_range_threshold = adx_range_threshold
        self.cache_duration_seconds = cache_duration_seconds

        # State
        self.current_regime = MarketRegime.UNKNOWN
        self.last_detection_time: Optional[datetime] = None
        self.regime_history: List[Tuple[datetime, MarketRegime]] = []

        # Detection metrics (for logging/debugging)
        self.last_adx: float = 0.0
        self.last_atr: float = 0.0
        self.last_bb_width: float = 0.0

    def detect_regime(self, market_data: Dict) -> MarketRegime:
        """
        Detect current market regime from market data.

        Args:
            market_data: Dict with indicators (adx, atr, bb_upper, bb_lower, sma20, price)

        Returns:
            Detected MarketRegime
        """
        now = datetime.now()

        # Check cache
        if (self.last_detection_time and
            (now - self.last_detection_time).total_seconds() < self.cache_duration_seconds):
            return self.current_regime

        # Extract indicators
        adx = market_data.get('adx', 0)
        atr = market_data.get('atr', 0)
        price = market_data.get('price', 0)
        sma20 = market_data.get('sma20', price)
        sma50 = market_data.get('sma50', price)
        bb_upper = market_data.get('bb_upper', price * 1.02)
        bb_lower = market_data.get('bb_lower', price * 0.98)
        rsi = market_data.get('rsi', 50)
        macd = market_data.get('macd', 0)

        # Calculate BB width as percentage
        bb_width = ((bb_upper - bb_lower) / price * 100) if price > 0 else 0

        # Store for debugging
        self.last_adx = adx
        self.last_atr = atr
        self.last_bb_width = bb_width

        # Classification logic
        regime = self._classify_regime(
            adx=adx,
            price=price,
            sma20=sma20,
            sma50=sma50,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            bb_width=bb_width,
            atr=atr,
            rsi=rsi,
            macd=macd
        )

        # Update state
        self.current_regime = regime
        self.last_detection_time = now
        self.regime_history.append((now, regime))

        # Keep only last 100 entries
        if len(self.regime_history) > 100:
            self.regime_history = self.regime_history[-100:]

        logger.info(f"[REGIME] {self.symbol}: {regime.value} (ADX={adx:.1f}, ATR={atr:.4f}, BB%={bb_width:.2f}%)")

        return regime

    def _classify_regime(
        self,
        adx: float,
        price: float,
        sma20: float,
        sma50: float,
        bb_upper: float,
        bb_lower: float,
        bb_width: float,
        atr: float,
        rsi: float,
        macd: float
    ) -> MarketRegime:
        """
        Classify market regime based on indicators.

        Logic:
        1. If ADX > 25 → Trending
           - Price > SMA20 and SMA20 > SMA50 → TRENDING_UP
           - Price < SMA20 and SMA20 < SMA50 → TRENDING_DOWN
        2. If ADX < 20 → Ranging
           - BB width > 4% → CHOPPY (high volatility ranging)
           - BB width <= 4% → RANGE_BOUND (tight range)
        3. Otherwise → Use momentum (MACD, RSI) as tiebreaker
        """
        # Handle missing data
        if adx == 0 or price == 0:
            return MarketRegime.UNKNOWN

        # Strong trend detection
        if adx >= self.adx_trend_threshold:
            # Determine trend direction
            if price > sma20 and (sma50 == 0 or sma20 >= sma50):
                return MarketRegime.TRENDING_UP
            elif price < sma20 and (sma50 == 0 or sma20 <= sma50):
                return MarketRegime.TRENDING_DOWN
            else:
                # ADX high but mixed signals - use momentum
                if macd > 0 and rsi > 50:
                    return MarketRegime.TRENDING_UP
                elif macd < 0 and rsi < 50:
                    return MarketRegime.TRENDING_DOWN
                else:
                    return MarketRegime.CHOPPY

        # Weak trend / ranging
        if adx <= self.adx_range_threshold:
            # Check volatility within range
            if bb_width > 4.0:  # High volatility
                return MarketRegime.CHOPPY
            else:
                return MarketRegime.RANGE_BOUND

        # Middle ground (ADX between 20-25)
        # Use momentum as tiebreaker
        if macd > 0 and rsi > 55:
            return MarketRegime.TRENDING_UP
        elif macd < 0 and rsi < 45:
            return MarketRegime.TRENDING_DOWN
        else:
            return MarketRegime.CHOPPY

    def get_parameters(self) -> RegimeParameters:
        """Get trading parameters for current regime."""
        return REGIME_PARAMS.get(self.current_regime, REGIME_PARAMS[MarketRegime.UNKNOWN])

    def get_trade_parameters(self, base_stop_loss_pct: float = 2.0) -> Dict:
        """
        Get adjusted trade parameters based on current regime.

        Args:
            base_stop_loss_pct: Base stop loss percentage (default 2.0%)

        Returns:
            Dict with stop_loss, max_hold_hours, size_multiplier
        """
        params = self.get_parameters()
        return {
            'stop_loss': base_stop_loss_pct * params.stop_loss_multiplier,
            'max_hold_hours': params.max_hold_hours,
            'size_multiplier': params.size_multiplier,
            'regime': self.current_regime.value
        }

    def should_veto_trade(self, side: str, confidence: float) -> Tuple[bool, str]:
        """
        Check if current regime should veto a trade.

        Based on Qwen consultation: Don't veto, but reduce size in unfavorable conditions.

        Args:
            side: 'LONG' or 'SHORT'
            confidence: Raw confidence from LLM

        Returns:
            (should_veto: bool, reason: str)
        """
        # Never fully veto - Qwen recommendation
        # Instead, size adjustments handle regime risk

        # Exception: RANGE_BOUND with low confidence should be vetoed
        if self.current_regime == MarketRegime.RANGE_BOUND and confidence < 0.6:
            return True, f"RANGE_BOUND regime + low confidence ({confidence:.2f})"

        return False, ""

    def get_prompt_context(self) -> str:
        """Get regime context for LLM prompt."""
        params = self.get_parameters()
        return (
            f"\n=== MARKET REGIME: {self.current_regime.value} ===\n"
            f"ADX: {self.last_adx:.1f} | ATR: {self.last_atr:.4f} | BB Width: {self.last_bb_width:.2f}%\n"
            f"Adjusted Parameters:\n"
            f"  Stop Loss: {params.stop_loss_multiplier * 2.0:.1f}%\n"
            f"  Max Hold: {params.max_hold_hours:.0f}h\n"
            f"  Size Multiplier: {params.size_multiplier:.1f}x\n"
        )
