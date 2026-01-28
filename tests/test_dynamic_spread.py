#!/usr/bin/env python3
"""
Tests for Dynamic Spread logic in Grid MM v12

Tests the _calculate_dynamic_spread() method that maps ROC to spread levels.
"""

import pytest
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestDynamicSpread:
    """Test dynamic spread calculation based on ROC"""

    def _calculate_dynamic_spread(self, roc: float) -> float:
        """
        Mirror of the production logic for testing.

        Spread bands:
        | ROC (abs) | Spread | Rationale |
        |-----------|--------|-----------|
        | 0-5 bps   | 1.5 bps| Calm market, max fills |
        | 5-15 bps  | 3 bps  | Low volatility, balanced |
        | 15-30 bps | 6 bps  | Moderate volatility, protect |
        | 30-50 bps | 10 bps | High volatility, wide protection |
        | > 50 bps  | 15 bps | Fallback (pause logic handles this) |
        """
        abs_roc = abs(roc)

        if abs_roc < 5:
            spread = 1.5
        elif abs_roc < 15:
            spread = 3.0
        elif abs_roc < 30:
            spread = 6.0
        elif abs_roc < 50:
            spread = 10.0
        else:
            spread = 15.0

        return spread

    # ========== CALM MARKET (ROC 0-5 bps) ==========

    def test_calm_market_zero_roc(self):
        """ROC = 0 should give tightest spread (1.5 bps)"""
        assert self._calculate_dynamic_spread(0.0) == 1.5

    def test_calm_market_low_positive(self):
        """ROC = 2 bps should give tight spread"""
        assert self._calculate_dynamic_spread(2.0) == 1.5

    def test_calm_market_low_negative(self):
        """ROC = -3 bps should give tight spread (absolute value used)"""
        assert self._calculate_dynamic_spread(-3.0) == 1.5

    def test_calm_market_boundary(self):
        """ROC = 4.9 bps should still be calm market"""
        assert self._calculate_dynamic_spread(4.9) == 1.5

    # ========== LOW VOLATILITY (ROC 5-15 bps) ==========

    def test_low_vol_boundary_start(self):
        """ROC = 5 bps starts low volatility band"""
        assert self._calculate_dynamic_spread(5.0) == 3.0

    def test_low_vol_mid(self):
        """ROC = 10 bps mid-range low volatility"""
        assert self._calculate_dynamic_spread(10.0) == 3.0

    def test_low_vol_negative(self):
        """ROC = -10 bps (negative) should use absolute value"""
        assert self._calculate_dynamic_spread(-10.0) == 3.0

    def test_low_vol_boundary_end(self):
        """ROC = 14.9 bps should still be low volatility"""
        assert self._calculate_dynamic_spread(14.9) == 3.0

    # ========== MODERATE VOLATILITY (ROC 15-30 bps) ==========

    def test_moderate_vol_boundary_start(self):
        """ROC = 15 bps starts moderate volatility band"""
        assert self._calculate_dynamic_spread(15.0) == 6.0

    def test_moderate_vol_mid(self):
        """ROC = 22 bps mid-range moderate volatility"""
        assert self._calculate_dynamic_spread(22.0) == 6.0

    def test_moderate_vol_negative(self):
        """ROC = -25 bps (negative) should widen spread"""
        assert self._calculate_dynamic_spread(-25.0) == 6.0

    def test_moderate_vol_boundary_end(self):
        """ROC = 29.9 bps should still be moderate volatility"""
        assert self._calculate_dynamic_spread(29.9) == 6.0

    # ========== HIGH VOLATILITY (ROC 30-50 bps) ==========

    def test_high_vol_boundary_start(self):
        """ROC = 30 bps starts high volatility band"""
        assert self._calculate_dynamic_spread(30.0) == 10.0

    def test_high_vol_mid(self):
        """ROC = 40 bps mid-range high volatility"""
        assert self._calculate_dynamic_spread(40.0) == 10.0

    def test_high_vol_negative(self):
        """ROC = -45 bps (negative) should use wide spread"""
        assert self._calculate_dynamic_spread(-45.0) == 10.0

    def test_high_vol_boundary_end(self):
        """ROC = 49.9 bps should still be high volatility band"""
        assert self._calculate_dynamic_spread(49.9) == 10.0

    # ========== EXTREME VOLATILITY (ROC > 50 bps) ==========

    def test_extreme_vol_boundary(self):
        """ROC = 50 bps triggers fallback (pause logic handles this)"""
        assert self._calculate_dynamic_spread(50.0) == 15.0

    def test_extreme_vol_high(self):
        """ROC = 100 bps extreme volatility"""
        assert self._calculate_dynamic_spread(100.0) == 15.0

    def test_extreme_vol_negative(self):
        """ROC = -80 bps negative extreme"""
        assert self._calculate_dynamic_spread(-80.0) == 15.0

    # ========== EDGE CASES ==========

    def test_very_small_roc(self):
        """Very small ROC (0.01 bps) should give tight spread"""
        assert self._calculate_dynamic_spread(0.01) == 1.5

    def test_very_large_roc(self):
        """Very large ROC (1000 bps) should give fallback"""
        assert self._calculate_dynamic_spread(1000.0) == 15.0

    # ========== SPREAD ORDERING ==========

    def test_spread_increases_with_volatility(self):
        """Spreads should increase monotonically with ROC"""
        spreads = [
            self._calculate_dynamic_spread(0),    # 1.5
            self._calculate_dynamic_spread(5),    # 3.0
            self._calculate_dynamic_spread(15),   # 6.0
            self._calculate_dynamic_spread(30),   # 10.0
            self._calculate_dynamic_spread(50),   # 15.0
        ]
        # Each spread should be >= previous
        for i in range(1, len(spreads)):
            assert spreads[i] >= spreads[i-1], f"Spread at index {i} should be >= previous"

    def test_symmetry(self):
        """Positive and negative ROC should give same spread"""
        test_values = [1, 5, 10, 20, 35, 60]
        for val in test_values:
            assert self._calculate_dynamic_spread(val) == self._calculate_dynamic_spread(-val)


class TestDynamicSpreadIntegration:
    """Integration tests with actual GridMarketMaker classes"""

    def test_import_nado_grid_mm(self):
        """Verify we can import the GridMarketMakerNado class"""
        try:
            from scripts.grid_mm_nado_v8 import GridMarketMakerNado
            mm = GridMarketMakerNado()
            assert hasattr(mm, '_calculate_dynamic_spread')
        except ImportError as e:
            pytest.skip(f"Cannot import GridMarketMakerNado: {e}")

    def test_import_paradex_grid_mm(self):
        """Verify we can import the GridMarketMakerLive class (Paradex)"""
        try:
            from scripts.grid_mm_live import GridMarketMakerLive
            mm = GridMarketMakerLive()
            assert hasattr(mm, '_calculate_dynamic_spread')
        except ImportError as e:
            pytest.skip(f"Cannot import GridMarketMakerLive: {e}")

    def test_nado_spread_matches_test(self):
        """Verify Nado production logic matches test logic"""
        try:
            from scripts.grid_mm_nado_v8 import GridMarketMakerNado
            mm = GridMarketMakerNado()

            test_instance = TestDynamicSpread()
            test_rocs = [0, 3, 7, 12, 18, 25, 35, 45, 60, 100]

            for roc in test_rocs:
                expected = test_instance._calculate_dynamic_spread(roc)
                actual = mm._calculate_dynamic_spread(roc)
                assert actual == expected, f"Nado mismatch at ROC={roc}: expected {expected}, got {actual}"
        except ImportError as e:
            pytest.skip(f"Cannot import GridMarketMakerNado: {e}")

    def test_paradex_spread_matches_test(self):
        """Verify Paradex production logic matches test logic"""
        try:
            from scripts.grid_mm_live import GridMarketMakerLive
            mm = GridMarketMakerLive()

            test_instance = TestDynamicSpread()
            test_rocs = [0, 3, 7, 12, 18, 25, 35, 45, 60, 100]

            for roc in test_rocs:
                expected = test_instance._calculate_dynamic_spread(roc)
                actual = mm._calculate_dynamic_spread(roc)
                assert actual == expected, f"Paradex mismatch at ROC={roc}: expected {expected}, got {actual}"
        except ImportError as e:
            pytest.skip(f"Cannot import GridMarketMakerLive: {e}")


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

    def test_roc_with_180_samples_exactly(self):
        """ROC calculates correctly with exactly 180 samples"""
        from collections import deque
        price_history = deque(maxlen=360)

        # Add exactly 180 samples with 0.3% increase
        start_price = 3000.0
        end_price = 3009.0  # 0.3% higher

        for i in range(180):
            price = start_price + (end_price - start_price) * (i / 179)
            price_history.append(price)

        prices = list(price_history)
        roc = (prices[-1] - prices[-180]) / prices[-180] * 10000

        # Should be approximately +30 bps
        assert 25 < roc < 35, f"Expected ~30 bps, got {roc}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
