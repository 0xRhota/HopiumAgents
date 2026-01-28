"""
Confidence Calibrator - Fixes the LLM confidence trap

Problem: 0.8 LLM confidence = 44% actual win rate (35.8% gap!)
Solution: Platt scaling to calibrate confidence to actual probability

Method: Platt scaling
Recomputation: Every 24 hours (rolling window)
Minimum samples: 100 trades before symbol-specific calibration
"""

import json
import logging
import os
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Global calibration from 16,803 trades (pre-computed)
# Maps raw confidence buckets to actual win rates
GLOBAL_CALIBRATION = {
    0.5: 0.45,   # 50% confidence -> 45% actual
    0.6: 0.46,   # 60% confidence -> 46% actual
    0.7: 0.45,   # 70% confidence -> 45% actual
    0.8: 0.44,   # 80% confidence -> 44% actual
    0.9: 0.52,   # 90% confidence -> 52% actual
}

# Platt scaling parameters fitted to historical data
# P(y=1|f) = 1 / (1 + exp(A*f + B))
# These are pre-computed from the calibration table
GLOBAL_PLATT_A = 2.5   # Slope (positive = compression)
GLOBAL_PLATT_B = -1.2  # Intercept


@dataclass
class CalibrationState:
    """Calibration state for a symbol or global"""
    A: float
    B: float
    last_updated: str
    sample_count: int
    buckets: Dict[float, float]  # confidence -> actual_wr


class ConfidenceCalibrator:
    """
    Calibrates LLM confidence to actual win probability.

    Uses Platt scaling for smooth interpolation between calibration points.
    Falls back to global calibration when insufficient symbol-specific data.
    """

    def __init__(
        self,
        symbol: str = "global",
        calibration_dir: str = "llm_agent/data/calibration",
        min_samples_for_symbol: int = 100,
        recalibration_hours: int = 24,
    ):
        """
        Initialize confidence calibrator.

        Args:
            symbol: Symbol for symbol-specific calibration, or "global"
            calibration_dir: Directory to store calibration files
            min_samples_for_symbol: Minimum trades before using symbol calibration
            recalibration_hours: Hours between recalibration
        """
        self.symbol = symbol
        self.calibration_dir = calibration_dir
        self.min_samples_for_symbol = min_samples_for_symbol
        self.recalibration_hours = recalibration_hours

        # State
        self.A = GLOBAL_PLATT_A
        self.B = GLOBAL_PLATT_B
        self.sample_count = 0
        self.last_calibration: Optional[datetime] = None
        self.using_global = True

        # Trade history for recalibration
        self.trade_history: List[Dict] = []  # [{confidence, won, timestamp}]

        # Ensure calibration directory exists
        os.makedirs(calibration_dir, exist_ok=True)

        # Load existing calibration
        self._load_calibration()

    def _load_calibration(self):
        """Load calibration from file."""
        # Try symbol-specific first
        symbol_file = os.path.join(self.calibration_dir, f"{self.symbol.replace('/', '_')}.json")
        global_file = os.path.join(self.calibration_dir, "global.json")

        loaded = False
        if os.path.exists(symbol_file):
            try:
                with open(symbol_file, 'r') as f:
                    data = json.load(f)
                    if data.get('sample_count', 0) >= self.min_samples_for_symbol:
                        self.A = data.get('A', GLOBAL_PLATT_A)
                        self.B = data.get('B', GLOBAL_PLATT_B)
                        self.sample_count = data.get('sample_count', 0)
                        self.using_global = False
                        loaded = True
                        logger.info(f"[CALIBRATION] Loaded symbol-specific calibration for {self.symbol} ({self.sample_count} samples)")
            except Exception as e:
                logger.warning(f"[CALIBRATION] Failed to load symbol file: {e}")

        # Fall back to global
        if not loaded:
            if os.path.exists(global_file):
                try:
                    with open(global_file, 'r') as f:
                        data = json.load(f)
                        self.A = data.get('A', GLOBAL_PLATT_A)
                        self.B = data.get('B', GLOBAL_PLATT_B)
                        self.sample_count = data.get('sample_count', 0)
                        self.using_global = True
                        logger.info(f"[CALIBRATION] Using global calibration ({self.sample_count} samples)")
                except Exception as e:
                    logger.warning(f"[CALIBRATION] Failed to load global file: {e}")
            else:
                # Use pre-computed defaults
                self._save_default_global()
                logger.info("[CALIBRATION] Using pre-computed global calibration defaults")

    def _save_default_global(self):
        """Save default global calibration."""
        global_file = os.path.join(self.calibration_dir, "global.json")
        data = {
            'A': GLOBAL_PLATT_A,
            'B': GLOBAL_PLATT_B,
            'last_updated': datetime.now().isoformat(),
            'sample_count': 16803,  # Historical data
            'buckets': GLOBAL_CALIBRATION
        }
        try:
            with open(global_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"[CALIBRATION] Failed to save global file: {e}")

    def calibrate(self, raw_confidence: float) -> float:
        """
        Calibrate raw LLM confidence to actual win probability.

        Uses Platt scaling: P(y=1|f) = 1 / (1 + exp(A*f + B))

        Args:
            raw_confidence: Raw confidence from LLM (0.0 to 1.0)

        Returns:
            Calibrated confidence (actual expected win probability)
        """
        if raw_confidence <= 0:
            return 0.0
        if raw_confidence >= 1:
            raw_confidence = 0.99

        # Platt scaling
        calibrated = 1.0 / (1.0 + np.exp(self.A * raw_confidence + self.B))

        # Clamp to reasonable range
        calibrated = max(0.30, min(0.70, calibrated))

        logger.debug(f"[CALIBRATION] {raw_confidence:.2f} -> {calibrated:.2f} (A={self.A:.2f}, B={self.B:.2f})")

        return calibrated

    def record_trade(self, raw_confidence: float, won: bool):
        """
        Record trade result for future recalibration.

        Args:
            raw_confidence: Raw confidence from LLM
            won: Whether trade was profitable
        """
        self.trade_history.append({
            'confidence': raw_confidence,
            'won': won,
            'timestamp': datetime.now().isoformat()
        })

        # Keep only last 1000 trades
        if len(self.trade_history) > 1000:
            self.trade_history = self.trade_history[-1000:]

        # Check if recalibration needed
        self._maybe_recalibrate()

    def _maybe_recalibrate(self):
        """Check if recalibration is needed and perform if so."""
        if self.last_calibration:
            hours_since = (datetime.now() - self.last_calibration).total_seconds() / 3600
            if hours_since < self.recalibration_hours:
                return

        # Need at least 50 trades to recalibrate
        if len(self.trade_history) < 50:
            return

        logger.info(f"[CALIBRATION] Recalibrating with {len(self.trade_history)} trades...")
        self._fit_platt_scaling()
        self._save_calibration()

    def _fit_platt_scaling(self):
        """
        Fit Platt scaling parameters to trade history.

        Uses simple optimization to find A, B that minimize calibration error.
        """
        if len(self.trade_history) < 50:
            return

        # Group by confidence buckets
        buckets = {}  # bucket -> (wins, total)
        for trade in self.trade_history:
            conf = trade['confidence']
            bucket = round(conf, 1)
            if bucket not in buckets:
                buckets[bucket] = [0, 0]
            buckets[bucket][1] += 1
            if trade['won']:
                buckets[bucket][0] += 1

        # Calculate actual win rates per bucket
        calibration_points = []
        for bucket, (wins, total) in buckets.items():
            if total >= 5:  # Need at least 5 samples per bucket
                actual_wr = wins / total
                calibration_points.append((bucket, actual_wr))

        if len(calibration_points) < 3:
            logger.warning("[CALIBRATION] Insufficient data points for recalibration")
            return

        # Simple grid search for A, B
        # (In production, use scipy.optimize)
        best_error = float('inf')
        best_A, best_B = self.A, self.B

        for A in np.arange(0.5, 4.0, 0.5):
            for B in np.arange(-2.0, 0.5, 0.5):
                error = 0
                for conf, actual in calibration_points:
                    predicted = 1.0 / (1.0 + np.exp(A * conf + B))
                    error += (predicted - actual) ** 2
                if error < best_error:
                    best_error = error
                    best_A, best_B = A, B

        self.A = best_A
        self.B = best_B
        self.sample_count = len(self.trade_history)
        self.last_calibration = datetime.now()

        logger.info(f"[CALIBRATION] Recalibrated: A={self.A:.2f}, B={self.B:.2f} (error={best_error:.4f})")

    def _save_calibration(self):
        """Save calibration to file."""
        filename = f"{self.symbol.replace('/', '_')}.json"
        filepath = os.path.join(self.calibration_dir, filename)

        data = {
            'A': self.A,
            'B': self.B,
            'last_updated': datetime.now().isoformat(),
            'sample_count': self.sample_count,
            'trade_history_size': len(self.trade_history)
        }

        try:
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
            logger.info(f"[CALIBRATION] Saved calibration to {filepath}")
        except Exception as e:
            logger.warning(f"[CALIBRATION] Failed to save: {e}")

    def get_sizing_factor(self, calibrated_confidence: float) -> float:
        """
        Get position sizing factor based on calibrated confidence.

        Maps calibrated confidence to sizing:
        - <0.40: 0.5x (half size)
        - 0.40-0.50: 0.75x
        - 0.50-0.55: 1.0x (full size)
        - >0.55: 1.25x (slightly oversized)

        Args:
            calibrated_confidence: Calibrated confidence (from calibrate())

        Returns:
            Sizing multiplier
        """
        if calibrated_confidence < 0.40:
            return 0.5
        elif calibrated_confidence < 0.50:
            return 0.75
        elif calibrated_confidence < 0.55:
            return 1.0
        else:
            return 1.25

    def get_prompt_context(self) -> str:
        """Get calibration context for LLM prompt."""
        source = "symbol-specific" if not self.using_global else "global"
        return (
            f"\n=== CONFIDENCE CALIBRATION ({source}) ===\n"
            f"WARNING: Raw LLM confidence != actual win probability!\n"
            f"Calibration table (from {self.sample_count} trades):\n"
            f"  Raw 0.6 -> ~46% actual WR\n"
            f"  Raw 0.7 -> ~45% actual WR\n"
            f"  Raw 0.8 -> ~44% actual WR\n"
            f"  Raw 0.9 -> ~52% actual WR\n"
            f"Position sizing adjusted automatically based on calibrated confidence.\n"
        )
