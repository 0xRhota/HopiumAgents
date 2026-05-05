"""Tests for core/fills.py — FillEngine.

Trust-critical module. We aim for 100% logic coverage:
  - POST_ONLY rejection when crossing
  - POST_ONLY registers as resting maker when not crossing
  - LIMIT cross fills at level walk with taker fee
  - LIMIT non-cross registers as maker
  - MARKET walks book at top
  - Resting orders fill when matching trades flow
  - Fee bps applied correctly per-venue (rebate vs charge)
  - Funding lookup invoked at fill time
  - Queue ahead computed from book snapshot
"""
from __future__ import annotations

import pytest

from paper_sim.core.fills import FillEngine, PlaceResult
from paper_sim.core.types import (
    BookSnapshot,
    IntendedOrder,
    TradeTick,
    VenueFees,
)


PARADEX_FEES = VenueFees(venue="paradex", maker_bps=-0.5, taker_bps=2.0)
HL_FEES = VenueFees(venue="hyperliquid", maker_bps=2.0, taker_bps=5.0)


def make_book(venue="paradex", symbol="BTC-USD-PERP", ts=1.0,
              bids=((100.0, 1.0), (99.0, 2.0)),
              asks=((101.0, 1.0), (102.0, 2.0))):
    return BookSnapshot(ts=ts, venue=venue, symbol=symbol, bids=bids, asks=asks)


def make_engine(funding_bps: float = 0.0):
    fees = {"paradex": PARADEX_FEES, "hyperliquid": HL_FEES}
    return FillEngine(fees=fees, funding_lookup=lambda v, s, t: funding_bps)


# ─── POST_ONLY ─────────────────────────────────────────────────────────────

class TestPostOnly:
    def test_buy_at_best_bid_registers(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="POST_ONLY", price=100.0, size=0.5,
        )
        result = eng.place(order, make_book(), ts_arrived=0.5)
        assert result.fill is None
        assert result.resting_order_id is not None
        assert result.rejected_reason is None

    def test_buy_at_best_ask_rejected(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="POST_ONLY", price=101.0, size=0.5,
        )
        result = eng.place(order, make_book(), ts_arrived=0.5)
        assert result.rejected_reason == "post_only_would_cross"
        assert result.resting_order_id is None

    def test_buy_above_best_ask_rejected(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="POST_ONLY", price=101.5, size=0.5,
        )
        result = eng.place(order, make_book(), ts_arrived=0.5)
        assert result.rejected_reason == "post_only_would_cross"

    def test_sell_at_best_bid_rejected(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="SELL", type="POST_ONLY", price=100.0, size=0.5,
        )
        result = eng.place(order, make_book(), ts_arrived=0.5)
        assert result.rejected_reason == "post_only_would_cross"

    def test_sell_at_best_ask_registers(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="SELL", type="POST_ONLY", price=101.0, size=0.5,
        )
        result = eng.place(order, make_book(), ts_arrived=0.5)
        assert result.resting_order_id is not None


class TestPostOnlyQueueAhead:
    def test_queue_at_best_bid_includes_own_level(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="POST_ONLY", price=100.0, size=0.5,
        )
        eng.place(order, make_book(bids=((100.0, 3.0), (99.0, 1.0))), ts_arrived=0.5)
        q = eng.get_queue("paradex", "BTC-USD-PERP")
        opens = q.open_orders()
        assert len(opens) == 1
        assert opens[0].queue_ahead == 3.0

    def test_queue_below_best_includes_better_levels(self):
        # BUY at 99 → bids at >= 99 ahead = 100 (1.0) + 99 (2.0) = 3.0
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="POST_ONLY", price=99.0, size=0.5,
        )
        eng.place(order, make_book(bids=((100.0, 1.0), (99.0, 2.0))), ts_arrived=0.5)
        q = eng.get_queue("paradex", "BTC-USD-PERP")
        opens = q.open_orders()
        assert opens[0].queue_ahead == 3.0


# ─── LIMIT ─────────────────────────────────────────────────────────────────

class TestLimit:
    def test_limit_buy_cross_fills_at_ask(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="LIMIT", price=101.0, size=0.5,
        )
        result = eng.place(order, make_book(), ts_arrived=0.5)
        assert result.fill is not None
        assert result.fill.is_maker is False
        assert result.fill.price == 101.0
        assert result.fill.size == 0.5
        assert result.fill.fee_bps == 2.0  # paradex taker

    def test_limit_buy_walks_levels(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="LIMIT", price=102.0, size=2.0,
        )
        # Asks: 101@1, 102@2 → fill 1@101 + 1@102 = 2 total at avg 101.5
        result = eng.place(order, make_book(), ts_arrived=0.5)
        assert result.fill is not None
        assert result.fill.size == 2.0
        assert abs(result.fill.price - 101.5) < 1e-9

    def test_limit_buy_non_cross_registers(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="LIMIT", price=99.5, size=0.5,
        )
        result = eng.place(order, make_book(), ts_arrived=0.5)
        assert result.fill is None
        assert result.resting_order_id is not None


# ─── MARKET ────────────────────────────────────────────────────────────────

class TestMarket:
    def test_market_buy_walks_asks(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="MARKET", size=2.0,
        )
        result = eng.place(order, make_book(), ts_arrived=0.5)
        assert result.fill is not None
        assert result.fill.is_maker is False
        assert abs(result.fill.price - 101.5) < 1e-9
        assert result.fill.fee_bps == 2.0

    def test_market_sell_walks_bids(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="SELL", type="MARKET", size=3.0,
        )
        # Bids: 100@1, 99@2 → fill 1@100 + 2@99 = 3 total at avg (100+198)/3
        result = eng.place(order, make_book(), ts_arrived=0.5)
        assert result.fill is not None
        assert abs(result.fill.price - (298 / 3)) < 1e-9

    def test_market_empty_book_rejected(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="MARKET", size=1.0,
        )
        empty = make_book(asks=())
        result = eng.place(order, empty, ts_arrived=0.5)
        assert result.rejected_reason == "empty_book"

    def test_market_partial_fill_uses_available(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="MARKET", size=10.0,
        )
        # Asks total only 1+2 = 3; partial fill expected
        result = eng.place(order, make_book(), ts_arrived=0.5)
        assert result.fill is not None
        assert result.fill.size == 3.0


# ─── Resting → eventual maker fill via consume_trade ───────────────────────

class TestResting:
    def test_post_only_eventually_fills_on_matching_trade(self):
        eng = make_engine(funding_bps=1.5)
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="POST_ONLY", price=100.0, size=0.5,
            client_id="o1", strategy_tag="A",
        )
        eng.place(order, make_book(bids=((100.0, 0.0),)), ts_arrived=0.5)
        # Aggressor SELL at 100, size 0.5 → no queue, fills us
        fills = eng.consume_trade(TradeTick(
            ts=2.0, venue="paradex", symbol="BTC-USD-PERP",
            price=100.0, size=0.5, aggressor_side="SELL",
        ))
        assert len(fills) == 1
        f = fills[0]
        assert f.is_maker is True
        assert f.price == 100.0
        assert f.size == 0.5
        assert f.fee_bps == -0.5  # paradex maker rebate
        assert f.fee_paid_usd < 0  # rebate received
        assert f.funding_at_fill_bps == 1.5
        assert f.strategy_tag == "A"

    def test_resting_drains_queue_first(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="POST_ONLY", price=100.0, size=0.5,
            client_id="o1",
        )
        eng.place(order, make_book(bids=((100.0, 5.0),)), ts_arrived=0.5)

        # 3.0 of selling — drains queue, doesn't reach us
        fills = eng.consume_trade(TradeTick(
            ts=2.0, venue="paradex", symbol="BTC-USD-PERP",
            price=100.0, size=3.0, aggressor_side="SELL",
        ))
        assert fills == []

        # 3.0 more — drains remaining 2 of queue + 0.5 of us = 0.5 fill
        fills = eng.consume_trade(TradeTick(
            ts=3.0, venue="paradex", symbol="BTC-USD-PERP",
            price=100.0, size=3.0, aggressor_side="SELL",
        ))
        assert len(fills) == 1
        assert fills[0].size == 0.5

    def test_partial_fills_track_remaining(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="POST_ONLY", price=100.0, size=2.0,
            client_id="o1",
        )
        eng.place(order, make_book(bids=((100.0, 0.0),)), ts_arrived=0.5)

        fills = eng.consume_trade(TradeTick(
            ts=2.0, venue="paradex", symbol="BTC-USD-PERP",
            price=100.0, size=0.5, aggressor_side="SELL",
        ))
        assert len(fills) == 1
        assert fills[0].size == 0.5

        fills = eng.consume_trade(TradeTick(
            ts=3.0, venue="paradex", symbol="BTC-USD-PERP",
            price=100.0, size=1.5, aggressor_side="SELL",
        ))
        assert len(fills) == 1
        assert fills[0].size == 1.5


# ─── Fee accounting ────────────────────────────────────────────────────────

class TestFees:
    def test_paradex_maker_rebate_negative(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="POST_ONLY", price=100.0, size=1.0, client_id="o",
        )
        eng.place(order, make_book(bids=((100.0, 0.0),)), ts_arrived=0.5)
        fills = eng.consume_trade(TradeTick(
            ts=2.0, venue="paradex", symbol="BTC-USD-PERP",
            price=100.0, size=1.0, aggressor_side="SELL",
        ))
        # 100 * 1 * -0.5 / 10000 = -0.005 (we received 0.5 cents)
        assert abs(fills[0].fee_paid_usd - (-0.005)) < 1e-9

    def test_hyperliquid_maker_charge(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="hyperliquid", symbol="BTC",
            side="BUY", type="POST_ONLY", price=100.0, size=1.0, client_id="o",
        )
        book = BookSnapshot(ts=1.0, venue="hyperliquid", symbol="BTC",
                            bids=((100.0, 0.0),), asks=())
        eng.place(order, book, ts_arrived=0.5)
        fills = eng.consume_trade(TradeTick(
            ts=2.0, venue="hyperliquid", symbol="BTC",
            price=100.0, size=1.0, aggressor_side="SELL",
        ))
        # 100 * 1 * 2.0 / 10000 = 0.02 (we paid 2 cents)
        assert abs(fills[0].fee_paid_usd - 0.02) < 1e-9


# ─── Cancel ────────────────────────────────────────────────────────────────

class TestCancel:
    def test_cancel_resting(self):
        eng = make_engine()
        order = IntendedOrder(
            ts_decision=0.0, venue="paradex", symbol="BTC-USD-PERP",
            side="BUY", type="POST_ONLY", price=100.0, size=0.5,
            client_id="o1",
        )
        eng.place(order, make_book(), ts_arrived=0.5)
        assert eng.cancel_resting("paradex", "BTC-USD-PERP", "o1") is True

        fills = eng.consume_trade(TradeTick(
            ts=2.0, venue="paradex", symbol="BTC-USD-PERP",
            price=100.0, size=0.5, aggressor_side="SELL",
        ))
        assert fills == []
