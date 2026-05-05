"""Tests for core/types.py — frozen dataclass invariants and derived properties."""
from __future__ import annotations

import pytest
from dataclasses import FrozenInstanceError

from paper_sim.core.types import (
    BookSnapshot,
    FundingTick,
    IntendedOrder,
    PaperFill,
    Position,
    PortfolioSnapshot,
    TradeTick,
    VenueFees,
)


class TestBookSnapshot:
    def test_basic_construction(self):
        b = BookSnapshot(
            ts=1.0, venue="paradex", symbol="BTC-USD-PERP",
            bids=((100.0, 1.0), (99.5, 2.0)),
            asks=((100.5, 1.0), (101.0, 2.0)),
        )
        assert b.best_bid == 100.0
        assert b.best_ask == 100.5
        assert b.mid == 100.25
        assert abs(b.spread_bps - (0.5 / 100.25 * 10_000)) < 1e-9

    def test_empty_book(self):
        b = BookSnapshot(ts=1.0, venue="x", symbol="y", bids=(), asks=())
        assert b.best_bid is None
        assert b.best_ask is None
        assert b.mid is None
        assert b.spread_bps is None

    def test_one_sided(self):
        b = BookSnapshot(ts=1.0, venue="x", symbol="y", bids=((100.0, 1.0),), asks=())
        assert b.best_bid == 100.0
        assert b.best_ask is None
        assert b.mid is None

    def test_immutable(self):
        b = BookSnapshot(ts=1.0, venue="x", symbol="y", bids=(), asks=())
        with pytest.raises(FrozenInstanceError):
            b.ts = 2.0  # type: ignore[misc]


class TestIntendedOrder:
    def test_post_only_requires_price(self):
        with pytest.raises(ValueError, match="POST_ONLY requires a price"):
            IntendedOrder(
                ts_decision=1.0, venue="paradex", symbol="BTC", side="BUY",
                type="POST_ONLY", size=0.001, price=None,
            )

    def test_limit_requires_price(self):
        with pytest.raises(ValueError, match="LIMIT requires a price"):
            IntendedOrder(
                ts_decision=1.0, venue="paradex", symbol="BTC", side="BUY",
                type="LIMIT", size=0.001, price=None,
            )

    def test_market_no_price_ok(self):
        o = IntendedOrder(
            ts_decision=1.0, venue="paradex", symbol="BTC", side="BUY",
            type="MARKET", size=0.001,
        )
        assert o.price is None

    def test_zero_size_rejected(self):
        with pytest.raises(ValueError, match="size must be positive"):
            IntendedOrder(
                ts_decision=1.0, venue="paradex", symbol="BTC", side="BUY",
                type="MARKET", size=0,
            )

    def test_negative_size_rejected(self):
        with pytest.raises(ValueError, match="size must be positive"):
            IntendedOrder(
                ts_decision=1.0, venue="paradex", symbol="BTC", side="BUY",
                type="MARKET", size=-0.5,
            )


class TestTradeTick:
    def test_construction(self):
        t = TradeTick(ts=1.0, venue="hl", symbol="BTC", price=100.0, size=0.5,
                      aggressor_side="BUY")
        assert t.aggressor_side == "BUY"


class TestFundingTick:
    def test_construction(self):
        f = FundingTick(ts=1.0, venue="hl", symbol="BTC", rate_bps_per_8h=2.5)
        assert f.rate_bps_per_8h == 2.5
        assert f.next_settlement_ts is None


class TestPaperFill:
    def test_construction(self):
        f = PaperFill(
            fill_id="abc", ts_decision=1.0, ts_arrived=1.2, ts_filled=2.0,
            venue="paradex", symbol="BTC-USD-PERP", side="BUY", price=100.0,
            size=0.001, is_maker=True, fee_bps=-0.5, fee_paid_usd=-0.0005,
            funding_at_fill_bps=1.0, queue_ahead_at_arrival=5.0,
        )
        assert f.is_maker is True
        assert f.adverse_drift_bps_t30 is None  # filled in later


class TestPortfolioSnapshot:
    def test_construction(self):
        p = PortfolioSnapshot(
            ts=1.0, account="A", equity=5000.0, cash=4500.0,
            positions=(Position(venue="paradex", symbol="BTC", side="BUY",
                                size=0.001, entry_price=80000.0, entry_ts=0.5),),
            cumulative_fees_paid=0.5, cumulative_adverse_cost=0.2,
            cumulative_funding_paid=0.0,
        )
        assert len(p.positions) == 1


class TestVenueFees:
    def test_paradex_rebate(self):
        f = VenueFees(venue="paradex", maker_bps=-0.5, taker_bps=2.0)
        assert f.maker_bps < 0  # paradex pays makers
