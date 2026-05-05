"""Tests for core/queue.py — queue position tracker.

Critical invariants:
  - Order with queue_ahead=N requires N+S volume to drain before/our S fills.
  - Only OPPOSITE aggressor flow can fill us (we're the maker).
  - FIFO within a price level; price priority across levels.
"""
from __future__ import annotations

import pytest

from paper_sim.core.queue import QueuePositionTracker


@pytest.fixture
def q():
    return QueuePositionTracker("paradex", "BTC-USD-PERP")


class TestRegistration:
    def test_register_and_list(self, q):
        q.register("o1", "BUY", 100.0, 1.0, queue_ahead=2.0, ts_arrived=1.0)
        opens = q.open_orders()
        assert len(opens) == 1
        assert opens[0].order_id == "o1"
        assert opens[0].size_remaining == 1.0

    def test_register_invalid_side(self, q):
        with pytest.raises(ValueError):
            q.register("o1", "UP", 100.0, 1.0, queue_ahead=0.0, ts_arrived=1.0)

    def test_cancel(self, q):
        q.register("o1", "BUY", 100.0, 1.0, queue_ahead=0.0, ts_arrived=1.0)
        assert q.cancel("o1") is True
        assert len(q.open_orders()) == 0

    def test_cancel_nonexistent(self, q):
        assert q.cancel("nope") is False


class TestFillMatching:
    def test_buy_fills_only_on_sell_aggressor(self, q):
        # Our BUY POST_ONLY at 100. We're a maker; only a SELL aggressor
        # (selling into bids) can fill us.
        q.register("buy1", "BUY", 100.0, 1.0, queue_ahead=0.0, ts_arrived=1.0)

        # Aggressor BUY at 100 → consuming asks → CANNOT fill our BUY
        fills = q.consume_trade(trade_price=100.0, trade_size=5.0, aggressor_side="BUY")
        assert fills == []

        # Aggressor SELL at 100 → consuming bids → CAN fill our BUY
        fills = q.consume_trade(trade_price=100.0, trade_size=1.0, aggressor_side="SELL")
        assert fills == [("buy1", 1.0)]

    def test_sell_fills_only_on_buy_aggressor(self, q):
        q.register("sell1", "SELL", 101.0, 1.0, queue_ahead=0.0, ts_arrived=1.0)

        fills = q.consume_trade(trade_price=101.0, trade_size=5.0, aggressor_side="SELL")
        assert fills == []

        fills = q.consume_trade(trade_price=101.0, trade_size=1.0, aggressor_side="BUY")
        assert fills == [("sell1", 1.0)]


class TestQueuePosition:
    def test_drains_queue_before_filling_us(self, q):
        # BUY POST_ONLY at 100, with 5.0 ahead of us
        q.register("o1", "BUY", 100.0, 1.0, queue_ahead=5.0, ts_arrived=1.0)

        # 3.0 of selling pressure → drains queue, doesn't reach us yet
        fills = q.consume_trade(100.0, 3.0, "SELL")
        assert fills == []
        assert q.get_queue_ahead("o1") == 2.0

        # Another 3.0 → drains remaining 2.0 of queue, fills 1.0 of us
        fills = q.consume_trade(100.0, 3.0, "SELL")
        assert fills == [("o1", 1.0)]

    def test_partial_fill(self, q):
        # BUY 5.0 at 100, no queue
        q.register("big", "BUY", 100.0, 5.0, queue_ahead=0.0, ts_arrived=1.0)

        fills = q.consume_trade(100.0, 2.0, "SELL")
        assert fills == [("big", 2.0)]

        # Order still open with 3.0 remaining
        opens = q.open_orders()
        assert len(opens) == 1
        assert opens[0].size_remaining == 3.0


class TestPriceReachability:
    def test_buy_at_99_fills_only_when_trade_price_le_99(self, q):
        q.register("buy99", "BUY", 99.0, 1.0, queue_ahead=0.0, ts_arrived=1.0)

        # Trade at 100 (above our price) → cannot fill us
        fills = q.consume_trade(100.0, 1.0, "SELL")
        assert fills == []

        # Trade at 99 → can fill
        fills = q.consume_trade(99.0, 1.0, "SELL")
        assert fills == [("buy99", 1.0)]

    def test_sell_at_101_fills_only_when_trade_price_ge_101(self, q):
        q.register("s", "SELL", 101.0, 1.0, queue_ahead=0.0, ts_arrived=1.0)

        fills = q.consume_trade(100.0, 1.0, "BUY")
        assert fills == []

        fills = q.consume_trade(101.0, 1.0, "BUY")
        assert fills == [("s", 1.0)]


class TestPriority:
    def test_buy_better_price_first(self, q):
        # Two BUY orders — 100 and 99. Aggressor SELL hits both prices in turn.
        q.register("better", "BUY", 100.0, 1.0, queue_ahead=0.0, ts_arrived=1.0)
        q.register("worse", "BUY", 99.0, 1.0, queue_ahead=0.0, ts_arrived=1.0)

        # SELL at 99 (deep print) consumes 1.5 of liquidity. The 100-bid clears first
        # (better price), then 0.5 of the 99-bid.
        fills = q.consume_trade(99.0, 1.5, "SELL")
        assert ("better", 1.0) in fills
        assert ("worse", 0.5) in fills

    def test_fifo_within_level(self, q):
        # Two BUYs at same price — the first registered fills first
        q.register("first", "BUY", 100.0, 1.0, queue_ahead=0.0, ts_arrived=1.0)
        q.register("second", "BUY", 100.0, 1.0, queue_ahead=0.0, ts_arrived=2.0)

        fills = q.consume_trade(100.0, 1.0, "SELL")
        assert fills == [("first", 1.0)]


class TestFillRemoval:
    def test_filled_orders_dropped(self, q):
        q.register("a", "BUY", 100.0, 1.0, queue_ahead=0.0, ts_arrived=1.0)
        q.consume_trade(100.0, 1.0, "SELL")
        assert len(q.open_orders()) == 0

    def test_partial_orders_remain(self, q):
        q.register("a", "BUY", 100.0, 5.0, queue_ahead=0.0, ts_arrived=1.0)
        q.consume_trade(100.0, 2.0, "SELL")
        opens = q.open_orders()
        assert len(opens) == 1
        assert opens[0].size_remaining == 3.0
