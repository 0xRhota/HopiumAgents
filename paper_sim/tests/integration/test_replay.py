"""Tests for venues/replay.py — recorded-stream playback and recording."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper_sim.core.types import FundingTick, TradeTick
from paper_sim.venues.base import BookDelta, BookFullSnapshot
from paper_sim.venues.replay import RecorderVenue, ReplayVenue


FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_replay.jsonl"


@pytest.mark.asyncio
async def test_replay_yields_filtered_events():
    v = ReplayVenue(venue="test", recording_path=FIXTURE, speed=0.0)
    await v.connect()

    events = []
    async for e in v.stream(["BTC"]):
        events.append(e)

    assert len(events) == 4  # ETH excluded
    assert isinstance(events[0], BookFullSnapshot)
    assert isinstance(events[1], BookDelta)
    assert isinstance(events[2], TradeTick)
    assert isinstance(events[3], FundingTick)


@pytest.mark.asyncio
async def test_replay_filters_by_symbol():
    v = ReplayVenue(venue="test", recording_path=FIXTURE, speed=0.0)
    await v.connect()

    events = []
    async for e in v.stream(["ETH"]):
        events.append(e)

    assert len(events) == 1
    assert isinstance(events[0], TradeTick)
    assert events[0].symbol == "ETH"


@pytest.mark.asyncio
async def test_replay_missing_file():
    v = ReplayVenue(venue="test", recording_path="/nonexistent/path.jsonl",
                    speed=0.0)
    with pytest.raises(FileNotFoundError):
        await v.connect()


@pytest.mark.asyncio
async def test_replay_book_event_fields():
    v = ReplayVenue(venue="paradex", recording_path=FIXTURE, speed=0.0)
    await v.connect()

    events = []
    async for e in v.stream(["BTC"]):
        events.append(e)

    snap = events[0]
    assert isinstance(snap, BookFullSnapshot)
    assert snap.venue == "paradex"
    assert snap.bids == ((100.0, 1.0), (99.0, 2.0))
    assert snap.asks == ((101.0, 1.0), (102.0, 2.0))


def test_recorder_writes_then_replay_reads(tmp_path):
    out = tmp_path / "rec.jsonl"
    rec = RecorderVenue(out)
    rec.record(BookFullSnapshot(ts=1.0, venue="x", symbol="BTC",
                                bids=((100.0, 1.0),), asks=((101.0, 1.0),)))
    rec.record(TradeTick(ts=2.0, venue="x", symbol="BTC", price=100.5,
                         size=0.5, aggressor_side="BUY"))
    rec.record(FundingTick(ts=3.0, venue="x", symbol="BTC",
                           rate_bps_per_8h=1.5))
    rec.record(BookDelta(ts=4.0, venue="x", symbol="BTC", side="bid",
                         price=99.5, size=2.0))
    rec.close()

    lines = out.read_text().strip().splitlines()
    assert len(lines) == 4
    parsed = [json.loads(l) for l in lines]
    assert parsed[0]["kind"] == "book_snapshot"
    assert parsed[1]["kind"] == "trade"
    assert parsed[2]["kind"] == "funding"
    assert parsed[3]["kind"] == "book_delta"
