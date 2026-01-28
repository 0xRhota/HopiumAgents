"""
Adaptive Manager - Orchestrates all adaptive trading components

Components:
- RegimeDetector: Market condition classification
- ConfidenceCalibrator: LLM confidence correction
- CircuitBreaker: Loss sequence prevention

Integration points:
- get_trade_parameters(): Before making a trade decision
- calibrate_confidence(): After LLM returns confidence
- record_trade_result(): After trade closes
- get_prompt_context(): For LLM prompt enhancement
"""

import logging
from datetime import datetime
from typing import Dict, Optional, Tuple

from .regime_detector import RegimeDetector, MarketRegime
from .confidence_calibrator import ConfidenceCalibrator
from .circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


class AdaptiveManager:
    """
    Orchestrates adaptive trading system components.

    Provides unified interface for:
    - Pre-trade checks (circuit breaker, regime veto)
    - Confidence calibration
    - Trade parameter adjustment (stops, holds, sizing)
    - Post-trade recording
    """

    def __init__(
        self,
        symbol: str = "global",
        base_stop_loss_pct: float = 2.0,
        base_position_size: float = 10.0,
        calibration_dir: str = "llm_agent/data/calibration",
    ):
        """
        Initialize adaptive manager.

        Args:
            symbol: Trading symbol for per-symbol calibration
            base_stop_loss_pct: Base stop loss percentage
            base_position_size: Base position size in USD
            calibration_dir: Directory for calibration files
        """
        self.symbol = symbol
        self.base_stop_loss_pct = base_stop_loss_pct
        self.base_position_size = base_position_size

        # Initialize components
        self.regime_detector = RegimeDetector(symbol=symbol)
        self.confidence_calibrator = ConfidenceCalibrator(
            symbol=symbol,
            calibration_dir=calibration_dir
        )
        self.circuit_breaker = CircuitBreaker()

        # Track open position parameters (for honoring original regime)
        self.open_position_params: Dict[str, Dict] = {}

        logger.info(f"[ADAPTIVE] Initialized for {symbol}")

    def should_trade(self, raw_confidence: float, market_data: Dict) -> Tuple[bool, str, float]:
        """
        Pre-trade check: Should we enter this trade?

        Checks:
        1. Circuit breaker status
        2. Regime veto
        3. Calibrated confidence threshold

        Args:
            raw_confidence: Raw LLM confidence (0.0-1.0)
            market_data: Market data for regime detection

        Returns:
            (should_trade: bool, reason: str, adjusted_size_multiplier: float)
        """
        # Step 1: Check circuit breaker
        is_triggered, trigger_reason = self.circuit_breaker.is_triggered()
        if is_triggered:
            # Calibrate confidence for override check
            calibrated = self.confidence_calibrator.calibrate(raw_confidence)
            regime = self.regime_detector.detect_regime(market_data)

            # Check if override allowed
            allow_override, size_mult = self.circuit_breaker.should_allow_override(
                confidence=calibrated,
                regime=regime.value
            )

            if not allow_override:
                return False, f"Circuit breaker: {trigger_reason}", 0.0

            # Override allowed
            logger.warning(f"[ADAPTIVE] Circuit breaker override at {size_mult}x size")
            return True, "Override allowed", size_mult

        # Step 2: Detect regime
        regime = self.regime_detector.detect_regime(market_data)

        # Step 3: Calibrate confidence
        calibrated = self.confidence_calibrator.calibrate(raw_confidence)

        # Step 4: Check regime veto
        should_veto, veto_reason = self.regime_detector.should_veto_trade(
            side="LONG",  # Will be checked per-trade later
            confidence=calibrated
        )
        if should_veto:
            return False, f"Regime veto: {veto_reason}", 0.0

        # Step 5: Apply regime and calibration size adjustments
        regime_params = self.regime_detector.get_parameters()
        calibration_factor = self.confidence_calibrator.get_sizing_factor(calibrated)

        combined_size_mult = regime_params.size_multiplier * calibration_factor

        logger.info(f"[ADAPTIVE] Trade allowed: regime={regime.value}, "
                   f"calibrated_conf={calibrated:.2f}, size_mult={combined_size_mult:.2f}")

        return True, "", combined_size_mult

    def get_trade_parameters(self, symbol: str, market_data: Dict, raw_confidence: float) -> Dict:
        """
        Get adjusted trade parameters based on current conditions.

        Returns parameters respecting regime detection and calibration.

        Args:
            symbol: Trading symbol
            market_data: Market data for regime detection
            raw_confidence: Raw LLM confidence

        Returns:
            Dict with stop_loss, max_hold_hours, position_size, etc.
        """
        # Detect regime
        regime = self.regime_detector.detect_regime(market_data)
        regime_params = self.regime_detector.get_parameters()

        # Calibrate confidence
        calibrated = self.confidence_calibrator.calibrate(raw_confidence)
        sizing_factor = self.confidence_calibrator.get_sizing_factor(calibrated)

        # Calculate final values
        stop_loss = self.base_stop_loss_pct * regime_params.stop_loss_multiplier
        max_hold = regime_params.max_hold_hours
        position_size = self.base_position_size * regime_params.size_multiplier * sizing_factor

        params = {
            'symbol': symbol,
            'regime': regime.value,
            'raw_confidence': raw_confidence,
            'calibrated_confidence': calibrated,
            'stop_loss_pct': stop_loss,
            'max_hold_hours': max_hold,
            'position_size_usd': position_size,
            'size_multiplier': regime_params.size_multiplier * sizing_factor,
            'timestamp': datetime.now().isoformat()
        }

        # Store for this position (to honor original params on regime change)
        self.open_position_params[symbol] = params

        logger.info(f"[ADAPTIVE] Trade params for {symbol}:")
        logger.info(f"  Regime: {regime.value}")
        logger.info(f"  Confidence: {raw_confidence:.2f} -> {calibrated:.2f}")
        logger.info(f"  Stop: {stop_loss:.1f}% | Max Hold: {max_hold:.0f}h | Size: ${position_size:.2f}")

        return params

    def get_exit_parameters(self, symbol: str) -> Optional[Dict]:
        """
        Get exit parameters for an open position.

        IMPORTANT: Returns ORIGINAL parameters from entry, not current regime.
        This implements Qwen's recommendation to honor original trade setup.

        Args:
            symbol: Trading symbol

        Returns:
            Original trade parameters, or None if not found
        """
        return self.open_position_params.get(symbol)

    def calibrate_confidence(self, raw_confidence: float) -> float:
        """
        Calibrate raw LLM confidence to actual win probability.

        Args:
            raw_confidence: Raw confidence from LLM

        Returns:
            Calibrated confidence
        """
        return self.confidence_calibrator.calibrate(raw_confidence)

    def record_trade_result(self, symbol: str, pnl: float, raw_confidence: float):
        """
        Record trade result for learning.

        Updates:
        - Confidence calibrator (for recalibration)
        - Circuit breaker (for loss tracking)
        - Clears position params

        Args:
            symbol: Trading symbol
            pnl: Trade P&L in USD
            raw_confidence: Raw confidence used for the trade
        """
        won = pnl > 0

        # Record in calibrator
        self.confidence_calibrator.record_trade(raw_confidence, won)

        # Record in circuit breaker
        self.circuit_breaker.record_trade(pnl)

        # Clear position params
        if symbol in self.open_position_params:
            del self.open_position_params[symbol]

        logger.info(f"[ADAPTIVE] Recorded {symbol}: PnL=${pnl:.2f}, Won={won}")

    def record_override_result(self, won: bool):
        """Record result of circuit breaker override trade."""
        self.circuit_breaker.record_override_result(won)

    def get_prompt_context(self, market_data: Dict) -> str:
        """
        Get combined context from all components for LLM prompt.

        Args:
            market_data: Market data for regime detection

        Returns:
            Combined context string
        """
        # Ensure regime is detected
        self.regime_detector.detect_regime(market_data)

        context = "\n" + "=" * 60 + "\n"
        context += "=== ADAPTIVE TRADING SYSTEM ===\n"
        context += "=" * 60 + "\n"

        context += self.regime_detector.get_prompt_context()
        context += self.confidence_calibrator.get_prompt_context()
        context += self.circuit_breaker.get_prompt_context()

        return context

    def get_status(self) -> Dict:
        """Get status of all adaptive components."""
        return {
            'symbol': self.symbol,
            'regime': self.regime_detector.current_regime.value,
            'regime_adx': self.regime_detector.last_adx,
            'circuit_breaker': self.circuit_breaker.get_status(),
            'calibration_using_global': self.confidence_calibrator.using_global,
            'open_positions': list(self.open_position_params.keys())
        }

    def force_reset_circuit_breaker(self):
        """Force reset circuit breaker (manual override)."""
        self.circuit_breaker.force_reset()


# Convenience function for creating per-symbol managers
_managers: Dict[str, AdaptiveManager] = {}


def get_adaptive_manager(
    symbol: str,
    base_stop_loss_pct: float = 2.0,
    base_position_size: float = 10.0
) -> AdaptiveManager:
    """
    Get or create an AdaptiveManager for a symbol.

    Uses singleton pattern per symbol.

    Args:
        symbol: Trading symbol
        base_stop_loss_pct: Base stop loss percentage
        base_position_size: Base position size in USD

    Returns:
        AdaptiveManager instance
    """
    if symbol not in _managers:
        _managers[symbol] = AdaptiveManager(
            symbol=symbol,
            base_stop_loss_pct=base_stop_loss_pct,
            base_position_size=base_position_size
        )
    return _managers[symbol]
