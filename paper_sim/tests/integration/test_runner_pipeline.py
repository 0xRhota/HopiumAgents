"""Integration tests for the runner pipeline:
event from venue → runner state → strategy MarketState.

These tests would have caught the "briefing has 0 funding symbols even
though funding poll runs OK" bug we hit on 2026-05-05 — they pin down each
link in the chain so when something's wrong we know exactly where.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator, List

import pytest

from paper_sim.core.types import (
    BookSnapshot,
    FundingTick,
    IntendedOrder,
    PortfolioSnapshot,
    TradeTick,
)
from paper_sim.runner import PaperRunner, RunnerConfig
from paper_sim.strategies.base import MarketState, Strategy
from paper_sim.venues.base import (
    BookDelta,
    BookFullSnapshot,
    MarketEvent,
    VenueClient,
)


class _SpyStrategy(Strategy):
    """Captures the MarketState seen on each evaluate call."""
    name = "spy"

    def __init__(self):
        self.seen: List[MarketState] = []
        self._fired = False

    def venues(self):
        return ["test"]

    def symbols(self, venue):
        return ["BTC", "ETH"] if venue == "test" else []

    def evaluate(self, market, portfolio):
        self.seen.append(market)
        # Fire one POST_ONLY so we can verify orders flow too
        if not self._fired:
            book = market.books.get(("test", "BTC"))
            if book is not None and book.best_bid is not None:
                self._fired = True
                return [IntendedOrder(
                    ts_decision=market.ts, venue="test", symbol="BTC",
                    side="BUY", type="POST_ONLY",
                    price=book.best_bid, size=0.1, client_id="spy_buy",
                )]
        return []


class _ScriptedVenue(VenueClient):
    """Yields a fixed list of events in order. Stops after the last event."""

    def __init__(self, events: List[MarketEvent]):
        self._events = events
        self._closed = False

    @property
    def venue(self):
        return "test"

    async def connect(self):
        pass

    async def close(self):
        self._closed = True

    async def stream(self, symbols: List[str]) -> AsyncIterator[MarketEvent]:
        for e in self._events:
            if self._closed:
                return
            yield e
            # Tiny yield so other tasks (poll, decision tick) interleave
            await asyncio.sleep(0)


def _book_snap(ts, sym, bid=100.0, ask=101.0):
    return BookFullSnapshot(
        ts=ts, venue="test", symbol=sym,
        bids=((bid, 1.0), (bid - 1, 2.0)),
        asks=((ask, 1.0), (ask + 1, 2.0)),
    )


def _funding(ts, sym, bps=1.5):
    return FundingTick(ts=ts, venue="test", symbol=sym, rate_bps_per_8h=bps)


def _trade(ts, sym, price, size, side="BUY"):
    return TradeTick(ts=ts, venue="test", symbol=sym, price=price,
                     size=size, aggressor_side=side)


# ─── Pipeline links ────────────────────────────────────────────────────────

class TestEventToState:
    """Each event type must update the corresponding runner state."""

    @pytest.mark.asyncio
    async def test_book_snapshot_updates_books_dict(self, tmp_path):
        events = [_book_snap(1.0, "BTC"), _book_snap(2.0, "ETH"),
                  _trade(3.0, "BTC", 100.5, 0.5)]   # final trade triggers decision
        spy = _SpyStrategy()
        runner = PaperRunner(
            spy, {"test": _ScriptedVenue(events)},
            RunnerConfig(account="t", decision_interval_seconds=0.001,
                         ledger_dir=str(tmp_path), record_market_data=False),
            fees={"test": __import__("paper_sim.core.types", fromlist=["VenueFees"]).VenueFees("test", 0, 0)},
        )
        await runner.run()
        assert any(("test", "BTC") in m.books for m in spy.seen)
        assert any(("test", "ETH") in m.books for m in spy.seen)

    @pytest.mark.asyncio
    async def test_funding_tick_updates_funding_dict(self, tmp_path):
        events = [
            _book_snap(1.0, "BTC"),       # book first so strategy can fire
            _funding(2.0, "BTC", 2.5),
            _funding(3.0, "ETH", -1.0),
            _trade(4.0, "BTC", 100.5, 0.5),
        ]
        spy = _SpyStrategy()
        from paper_sim.core.types import VenueFees
        runner = PaperRunner(
            spy, {"test": _ScriptedVenue(events)},
            RunnerConfig(account="t", decision_interval_seconds=0.001,
                         ledger_dir=str(tmp_path), record_market_data=False),
            fees={"test": VenueFees("test", 0, 0)},
        )
        await runner.run()
        last_state = spy.seen[-1]
        assert ("test", "BTC") in last_state.funding
        assert ("test", "ETH") in last_state.funding
        assert last_state.funding[("test", "BTC")].rate_bps_per_8h == 2.5

    @pytest.mark.asyncio
    async def test_trade_appends_candles(self, tmp_path):
        # Trades must span at least 2 minute-buckets to close a candle.
        # Aggregator only appends a closed bucket when the next bucket opens.
        events = [
            _book_snap(1.0, "BTC"),
            _trade(2.0, "BTC", 100.5, 0.5),       # bucket 0 (minute=0)
            _trade(65.0, "BTC", 100.7, 0.3),      # bucket 1 — closes bucket 0
            _trade(125.0, "BTC", 100.9, 0.4),     # bucket 2 — closes bucket 1
        ]
        spy = _SpyStrategy()
        from paper_sim.core.types import VenueFees
        runner = PaperRunner(
            spy, {"test": _ScriptedVenue(events)},
            RunnerConfig(account="t", decision_interval_seconds=0.001,
                         ledger_dir=str(tmp_path), record_market_data=False),
            fees={"test": VenueFees("test", 0, 0)},
        )
        await runner.run()
        last_state = spy.seen[-1]
        # 1m candles should have at least 1 closed bucket appended
        closes_1m = last_state.candles.get(("test", "BTC", "1m_close"), [])
        assert len(closes_1m) >= 1, f"expected closed 1m candle, got {closes_1m}"


class TestBriefingResilience:
    """The Account C briefing builder must surface symbols when funding is
    available, even if a book is temporarily missing — and vice versa.

    Today's bug: builder required BOTH funding AND book; transient missing
    books blanked the entire briefing.
    """

    def test_builder_includes_funding_only_symbols(self):
        from paper_sim.strategies.account_c_llm_scout import AccountCLLMScout, AccountCConfig
        s = AccountCLLMScout(
            llm_clients=[lambda b: [], lambda b: []],
            config=AccountCConfig(universe=["BTC-USD-PERP"]),
        )
        ms = MarketState(ts=10000.0)
        ms.funding[("paradex", "BTC-USD-PERP")] = FundingTick(
            ts=1.0, venue="paradex", symbol="BTC-USD-PERP", rate_bps_per_8h=2.0,
        )
        # No book set
        briefing = s._build_briefing(ms, _empty_portfolio())
        # Must include the funding symbol even with no book
        assert any(item["symbol"] == "BTC-USD-PERP"
                   for item in briefing["funding_per_symbol"]), \
            "briefing dropped a funding symbol because the book was missing"

    def test_builder_includes_book_only_symbols_with_no_funding(self):
        # Book present, funding absent — symbol not in funding_per_symbol but
        # also doesn't crash; movers may still surface from candles
        from paper_sim.strategies.account_c_llm_scout import AccountCLLMScout, AccountCConfig
        s = AccountCLLMScout(
            llm_clients=[lambda b: [], lambda b: []],
            config=AccountCConfig(universe=["BTC-USD-PERP"]),
        )
        ms = MarketState(ts=10000.0)
        ms.books[("paradex", "BTC-USD-PERP")] = BookSnapshot(
            ts=1.0, venue="paradex", symbol="BTC-USD-PERP",
            bids=((100.0, 1.0),), asks=((101.0, 1.0),),
        )
        briefing = s._build_briefing(ms, _empty_portfolio())
        # Should not raise; should produce a sensible (possibly empty-funding) briefing
        assert "funding_per_symbol" in briefing


def _empty_portfolio():
    return PortfolioSnapshot(
        ts=0.0, account="t", equity=5000.0, cash=5000.0, positions=(),
        cumulative_fees_paid=0.0, cumulative_adverse_cost=0.0,
        cumulative_funding_paid=0.0,
    )
