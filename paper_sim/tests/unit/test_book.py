"""Tests for core/book.py — L2 book maintenance."""
from __future__ import annotations

import pytest

from paper_sim.core.book import BookMaintainer


@pytest.fixture
def book():
    return BookMaintainer("paradex", "BTC-USD-PERP")


class TestSnapshot:
    def test_initial_state(self, book):
        snap = book.snapshot()
        assert snap.bids == ()
        assert snap.asks == ()
        assert snap.mid is None

    def test_apply_snapshot(self, book):
        book.apply_snapshot(
            ts=1.0,
            bids=[(100.0, 1.0), (99.0, 2.0), (98.0, 3.0)],
            asks=[(101.0, 1.0), (102.0, 2.0)],
        )
        snap = book.snapshot()
        assert snap.bids[0] == (100.0, 1.0)  # best bid first (descending)
        assert snap.asks[0] == (101.0, 1.0)  # best ask first (ascending)
        assert snap.mid == 100.5
        assert snap.ts == 1.0

    def test_snapshot_zero_size_dropped(self, book):
        book.apply_snapshot(ts=1.0, bids=[(100.0, 0.0), (99.0, 1.0)], asks=[])
        snap = book.snapshot()
        assert snap.bids == ((99.0, 1.0),)


class TestDeltas:
    def test_add_bid(self, book):
        book.apply_delta(ts=1.0, side="bid", price=100.0, size=1.0)
        assert book.size_at("bid", 100.0) == 1.0

    def test_update_bid_size(self, book):
        book.apply_delta(ts=1.0, side="bid", price=100.0, size=1.0)
        book.apply_delta(ts=2.0, side="bid", price=100.0, size=3.5)
        assert book.size_at("bid", 100.0) == 3.5

    def test_delete_bid_via_zero_size(self, book):
        book.apply_delta(ts=1.0, side="bid", price=100.0, size=1.0)
        book.apply_delta(ts=2.0, side="bid", price=100.0, size=0.0)
        assert book.size_at("bid", 100.0) == 0.0

    def test_invalid_side(self, book):
        with pytest.raises(ValueError, match="side must be"):
            book.apply_delta(ts=1.0, side="oops", price=100.0, size=1.0)


class TestQueueAhead:
    def test_buy_at_top_no_one_ahead(self, book):
        # Book: bids 100 (1.0), 99 (2.0). Place BUY POST_ONLY at 100.
        # "Ahead of us" = bids at >= 100 = 1.0 (the level we're joining).
        book.apply_snapshot(ts=1.0, bids=[(100.0, 1.0), (99.0, 2.0)], asks=[])
        assert book.cumulative_size_at_or_better("bid", 100.0) == 1.0

    def test_buy_below_top_only_better_count(self, book):
        # Place BUY POST_ONLY at 99. Bids ahead = 100 (1.0) + 99 (2.0) = 3.0
        # All bids >= 99 are "better" or "tied" → ahead of us.
        book.apply_snapshot(ts=1.0, bids=[(100.0, 1.0), (99.0, 2.0), (98.0, 3.0)], asks=[])
        assert book.cumulative_size_at_or_better("bid", 99.0) == 3.0

    def test_sell_at_top(self, book):
        book.apply_snapshot(ts=1.0, bids=[], asks=[(101.0, 1.0), (102.0, 2.0)])
        assert book.cumulative_size_at_or_better("ask", 101.0) == 1.0

    def test_sell_below_top(self, book):
        # SELL POST_ONLY at 102 → ahead = asks at <= 102 = 1+2 = 3
        book.apply_snapshot(ts=1.0, bids=[], asks=[(101.0, 1.0), (102.0, 2.0), (103.0, 3.0)])
        assert book.cumulative_size_at_or_better("ask", 102.0) == 3.0

    def test_no_one_ahead(self, book):
        # BUY at 99 with empty book → 0 ahead
        book.apply_snapshot(ts=1.0, bids=[], asks=[])
        assert book.cumulative_size_at_or_better("bid", 99.0) == 0.0


class TestTruncation:
    def test_max_levels_enforced(self):
        b = BookMaintainer("v", "s", max_levels=3)
        # Add 5 bids; only top 3 (highest prices) should remain
        for p in [100.0, 99.0, 98.0, 97.0, 96.0]:
            b.apply_delta(ts=1.0, side="bid", price=p, size=1.0)
        snap = b.snapshot(top_n=10)
        assert len(snap.bids) == 3
        assert {p for p, _ in snap.bids} == {100.0, 99.0, 98.0}


class TestReset:
    def test_clears_state(self, book):
        book.apply_snapshot(ts=1.0, bids=[(100.0, 1.0)], asks=[(101.0, 1.0)])
        book.reset()
        snap = book.snapshot()
        assert snap.bids == ()
        assert snap.asks == ()
