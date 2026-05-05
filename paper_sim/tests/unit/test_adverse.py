"""Tests for core/adverse.py — adverse selection tracker."""
from __future__ import annotations

import pytest

from paper_sim.core.adverse import AdverseSelectionTracker


@pytest.fixture
def tracker():
    return AdverseSelectionTracker(window_seconds=30.0)


def constant_mid(price: float):
    """Helper: returns a get_mid callable that always returns `price`."""
    return lambda venue, symbol: price


def none_mid(venue, symbol):
    return None


class TestRegistration:
    def test_register_and_pending(self, tracker):
        tracker.register("f1", fill_ts=100.0, fill_price=80000.0,
                         side="BUY", venue="paradex", symbol="BTC")
        assert tracker.pending_count() == 1

    def test_invalid_side(self, tracker):
        with pytest.raises(ValueError):
            tracker.register("f1", 100.0, 80000.0, "long", "paradex", "BTC")

    def test_zero_window_rejected(self):
        with pytest.raises(ValueError):
            AdverseSelectionTracker(window_seconds=0)


class TestPolling:
    def test_no_measurements_due_yet(self, tracker):
        tracker.register("f1", 100.0, 80000.0, "BUY", "paradex", "BTC")
        results = tracker.poll(current_ts=110.0, get_mid=constant_mid(80000.0))
        assert results == []
        assert tracker.pending_count() == 1

    def test_measurement_fires_after_window(self, tracker):
        tracker.register("f1", 100.0, 80000.0, "BUY", "paradex", "BTC")
        results = tracker.poll(current_ts=130.0, get_mid=constant_mid(80000.0))
        assert results == [("f1", 0.0)]
        assert tracker.pending_count() == 0

    def test_buy_favorable_drift(self, tracker):
        # Bought at 100, mid is now 100.5 → +50 bps (favorable for buyer)
        tracker.register("f1", 0.0, 100.0, "BUY", "v", "s")
        results = tracker.poll(current_ts=30.0, get_mid=constant_mid(100.5))
        assert len(results) == 1
        assert abs(results[0][1] - 50.0) < 1e-6

    def test_buy_adverse_drift(self, tracker):
        # Bought at 100, mid drops to 99 → -100 bps (adverse for buyer)
        tracker.register("f1", 0.0, 100.0, "BUY", "v", "s")
        results = tracker.poll(current_ts=30.0, get_mid=constant_mid(99.0))
        assert abs(results[0][1] - (-100.0)) < 1e-6

    def test_sell_favorable_drift(self, tracker):
        # Sold at 100, mid drops to 99 → +100 bps (favorable for seller)
        tracker.register("f1", 0.0, 100.0, "SELL", "v", "s")
        results = tracker.poll(current_ts=30.0, get_mid=constant_mid(99.0))
        assert abs(results[0][1] - 100.0) < 1e-6

    def test_sell_adverse_drift(self, tracker):
        # Sold at 100, mid rises to 101 → -100 bps (adverse for seller)
        tracker.register("f1", 0.0, 100.0, "SELL", "v", "s")
        results = tracker.poll(current_ts=30.0, get_mid=constant_mid(101.0))
        assert abs(results[0][1] - (-100.0)) < 1e-6

    def test_no_mid_drops_measurement(self, tracker):
        tracker.register("f1", 0.0, 100.0, "BUY", "v", "s")
        results = tracker.poll(current_ts=30.0, get_mid=none_mid)
        assert results == []
        assert tracker.pending_count() == 0  # dropped, not retained


class TestMultiple:
    def test_multiple_fills_independent(self, tracker):
        tracker.register("a", 0.0, 100.0, "BUY", "v", "s")
        tracker.register("b", 5.0, 200.0, "SELL", "v", "s")
        # At t=20, a not yet due (needs 30), b not yet due (needs 35)
        results = tracker.poll(20.0, constant_mid(150.0))
        assert results == []
        assert tracker.pending_count() == 2

        # At t=30, a is due
        results = tracker.poll(30.0, constant_mid(105.0))
        assert len(results) == 1
        assert results[0][0] == "a"
        assert abs(results[0][1] - 500.0) < 1e-6  # +5% drift = 500 bps

        # At t=35, b is due
        results = tracker.poll(35.0, constant_mid(195.0))
        assert len(results) == 1
        assert results[0][0] == "b"
        # SELL at 200, current 195 → favorable 250 bps
        assert abs(results[0][1] - 250.0) < 1e-6
