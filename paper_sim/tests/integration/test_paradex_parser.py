"""Tests for Paradex WS message parsing.

Exercises ParadexVenue._dispatch with realistic message shapes (captured
from the live API). These tests would have caught the 2026-05-05 bug where
we read top-level `bids`/`asks` instead of `data.inserts` with side fields.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from paper_sim.core.types import FundingTick, TradeTick
from paper_sim.venues.base import BookDelta, BookFullSnapshot
from paper_sim.venues.paradex import ParadexVenue


@pytest.fixture
def venue():
    v = ParadexVenue()
    return v


def _drain(queue):
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


@pytest.mark.asyncio
async def test_orderbook_snapshot_inserts_become_book_full_snapshot(venue):
    venue._event_queue = asyncio.Queue()
    raw = json.dumps({
        "jsonrpc": "2.0", "method": "subscription",
        "params": {
            "channel": "order_book.NEAR-USD-PERP.snapshot@15@100ms",
            "data": {
                "seq_no": 1, "market": "NEAR-USD-PERP",
                "last_updated_at": 1778076521772,
                "update_type": "s",
                "inserts": [
                    {"side": "BUY", "price": "1.424", "size": "398.6"},
                    {"side": "BUY", "price": "1.423", "size": "589.9"},
                    {"side": "SELL", "price": "1.425", "size": "200.0"},
                    {"side": "SELL", "price": "1.426", "size": "150.0"},
                ],
            }
        }
    })
    await venue._dispatch(raw)
    events = _drain(venue._event_queue)
    assert len(events) == 1
    e = events[0]
    assert isinstance(e, BookFullSnapshot)
    assert e.symbol == "NEAR-USD-PERP"
    assert (1.424, 398.6) in e.bids
    assert (1.423, 589.9) in e.bids
    assert (1.425, 200.0) in e.asks
    assert (1.426, 150.0) in e.asks


@pytest.mark.asyncio
async def test_orderbook_delta_yields_book_delta_per_level(venue):
    venue._event_queue = asyncio.Queue()
    raw = json.dumps({
        "jsonrpc": "2.0", "method": "subscription",
        "params": {
            "channel": "order_book.NEAR-USD-PERP.snapshot@15@100ms",
            "data": {
                "seq_no": 2, "market": "NEAR-USD-PERP",
                "last_updated_at": 1778076521900,
                "update_type": "d",
                "inserts": [
                    {"side": "BUY", "price": "1.420", "size": "100.0"},
                ],
                "updates": [
                    {"side": "SELL", "price": "1.425", "size": "180.0"},
                ],
                "deletes": [
                    {"side": "BUY", "price": "1.423", "size": "0"},
                ],
            }
        }
    })
    await venue._dispatch(raw)
    events = _drain(venue._event_queue)
    deltas = [e for e in events if isinstance(e, BookDelta)]
    assert len(deltas) == 3
    by_side_price = {(d.side, d.price): d.size for d in deltas}
    assert by_side_price[("bid", 1.420)] == 100.0       # insert
    assert by_side_price[("ask", 1.425)] == 180.0       # update
    assert by_side_price[("bid", 1.423)] == 0.0         # delete


@pytest.mark.asyncio
async def test_trade_message(venue):
    venue._event_queue = asyncio.Queue()
    raw = json.dumps({
        "jsonrpc": "2.0", "method": "subscription",
        "params": {
            "channel": "trades.BTC-USD-PERP",
            "data": {
                "id": "abc", "market": "BTC-USD-PERP",
                "price": "80000.5", "size": "0.001",
                "side": "BUY", "created_at": 1778076521000,
            }
        }
    })
    await venue._dispatch(raw)
    events = _drain(venue._event_queue)
    trades = [e for e in events if isinstance(e, TradeTick)]
    assert len(trades) == 1
    assert trades[0].price == 80000.5
    assert trades[0].aggressor_side == "BUY"


@pytest.mark.asyncio
async def test_malformed_message_does_not_crash(venue):
    """Robustness — bad JSON or unknown channels must not raise."""
    venue._event_queue = asyncio.Queue()
    # Truly invalid JSON
    await venue._dispatch("not json")
    # Missing channel field — should produce no events
    await venue._dispatch(json.dumps({"params": {"data": {}}}))
    # No exception thrown is the assertion


@pytest.mark.asyncio
async def test_orderbook_snapshot_handles_empty_book(venue):
    """Edge case — venue can send a snapshot with no levels.

    Should NOT silently produce a useless book; we accept it but the snapshot
    has empty bids/asks → mid=None. Strategies must handle mid=None defensively.
    """
    venue._event_queue = asyncio.Queue()
    raw = json.dumps({
        "jsonrpc": "2.0", "method": "subscription",
        "params": {
            "channel": "order_book.X-USD-PERP.snapshot@15@100ms",
            "data": {
                "seq_no": 1, "market": "X-USD-PERP",
                "last_updated_at": 1778076521772,
                "update_type": "s",
                "inserts": [],
            }
        }
    })
    await venue._dispatch(raw)
    events = _drain(venue._event_queue)
    assert len(events) == 1
    assert isinstance(events[0], BookFullSnapshot)
    assert events[0].bids == ()
    assert events[0].asks == ()
