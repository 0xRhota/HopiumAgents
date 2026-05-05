"""Integration test: end-to-end runner against a recorded fixture.

Verifies the full pipeline: venue events → book updates → strategy → fills →
ledger entries. Uses a deterministic ReplayVenue and a stub strategy that
fires a single POST_ONLY at startup.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper_sim.core.types import IntendedOrder, PortfolioSnapshot
from paper_sim.runner import PaperRunner, RunnerConfig
from paper_sim.strategies.base import MarketState, Strategy
from paper_sim.venues.replay import ReplayVenue


class _StubStrategy(Strategy):
    """Single-shot: fires one BUY POST_ONLY at the first decision cycle."""
    name = "stub"

    def __init__(self):
        self._fired = False

    def venues(self):
        return ["paradex"]

    def symbols(self, venue):
        return ["BTC"] if venue == "paradex" else []

    def evaluate(self, market, portfolio):
        if self._fired:
            return []
        book = market.books.get(("paradex", "BTC"))
        if book is None or book.best_bid is None:
            return []
        self._fired = True
        return [IntendedOrder(
            ts_decision=market.ts, venue="paradex", symbol="BTC",
            side="BUY", type="POST_ONLY",
            price=book.best_bid, size=0.5,
            client_id="test_buy_1",
        )]


@pytest.fixture
def fixture_with_filling_trade(tmp_path):
    """Create a replay file: book → BUY POST_ONLY at 100 → SELL aggressor fills it."""
    p = tmp_path / "scenario.jsonl"
    events = [
        {"kind": "book_snapshot", "ts": 1.0, "symbol": "BTC",
         "bids": [[100.0, 0.5], [99.0, 1.0]],     # 0.5 ahead of us at 100
         "asks": [[101.0, 1.0], [102.0, 2.0]]},
        # Funding tick so funding_at_fill records something
        {"kind": "funding", "ts": 1.5, "symbol": "BTC",
         "rate_bps_per_8h": 1.0},
        # Trade that drains 0.5 queue + fills our 0.5 (sell aggressor at 100)
        {"kind": "trade", "ts": 100.0, "symbol": "BTC",
         "price": 100.0, "size": 1.0, "aggressor_side": "SELL"},
    ]
    with open(p, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return p


@pytest.mark.asyncio
async def test_end_to_end_post_only_then_fill(tmp_path, fixture_with_filling_trade):
    config = RunnerConfig(
        account="test_e2e",
        starting_equity=5000.0,
        decision_interval_seconds=0.1,    # fire on first event
        ledger_dir=str(tmp_path),
        record_market_data=False,
    )
    runner = PaperRunner(
        strategy=_StubStrategy(),
        venue_clients={"paradex": ReplayVenue("paradex",
                                              fixture_with_filling_trade,
                                              speed=0.0)},
        config=config,
    )
    await runner.run()

    # Read the ledger
    ledger_path = tmp_path / "test_e2e_ledger.jsonl"
    assert ledger_path.exists()
    fills = [json.loads(l) for l in ledger_path.read_text().strip().splitlines()]
    assert len(fills) == 1, f"Expected 1 fill, got {len(fills)}: {fills}"
    f = fills[0]
    assert f["venue"] == "paradex"
    assert f["symbol"] == "BTC"
    assert f["side"] == "BUY"
    assert f["price"] == 100.0
    assert f["size"] == 0.5
    assert f["is_maker"] is True
    assert f["fee_bps"] == -0.5  # paradex maker rebate
    assert f["funding_at_fill_bps"] == 1.0


@pytest.mark.asyncio
async def test_runner_records_market_data(tmp_path, fixture_with_filling_trade):
    config = RunnerConfig(
        account="rec_test",
        decision_interval_seconds=0.1,
        ledger_dir=str(tmp_path),
        record_market_data=True,
    )
    runner = PaperRunner(
        strategy=_StubStrategy(),
        venue_clients={"paradex": ReplayVenue("paradex",
                                              fixture_with_filling_trade,
                                              speed=0.0)},
        config=config,
    )
    await runner.run()

    rec_path = tmp_path / "paradex_l2_recording.jsonl"
    assert rec_path.exists()
    lines = rec_path.read_text().strip().splitlines()
    # Should have all 3 input events recorded
    assert len(lines) >= 3
