"""
Adaptive Trading System

Components:
- RegimeDetector: Classifies market regime (TRENDING/CHOPPY/RANGE_BOUND)
- ConfidenceCalibrator: Calibrates LLM confidence to actual win rates
- CircuitBreaker: Prevents catastrophic loss sequences
- AdaptiveManager: Orchestrates all components
"""

from .regime_detector import RegimeDetector, MarketRegime
from .confidence_calibrator import ConfidenceCalibrator
from .circuit_breaker import CircuitBreaker
from .adaptive_manager import AdaptiveManager

__all__ = [
    'RegimeDetector',
    'MarketRegime',
    'ConfidenceCalibrator',
    'CircuitBreaker',
    'AdaptiveManager'
]
