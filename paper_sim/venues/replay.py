"""Replay venue — emits events from a recorded JSONL stream.

Used for:
  - Deterministic unit tests (synthetic fixtures)
  - Calibration replays of recorded live data
  - Backtest-style runs against archived books

File format: one JSON object per line, each must include 'kind' and 'ts'.
Kinds: 'book_snapshot' | 'book_delta' | 'trade' | 'funding'.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator, List, Optional

from paper_sim.core.types import FundingTick, TradeTick
from paper_sim.venues.base import (
    BookDelta,
    BookFullSnapshot,
    MarketEvent,
    VenueClient,
)


class ReplayVenue(VenueClient):
    """Replay events from a recorded file at controlled speed."""

    def __init__(self, venue: str, recording_path: str | Path,
                 speed: float = 0.0):
        """speed = 0 → as fast as possible. speed = 1.0 → real-time."""
        self._venue = venue
        self.path = Path(recording_path)
        self.speed = speed
        self._closed = False

    @property
    def venue(self) -> str:
        return self._venue

    async def connect(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"recording not found: {self.path}")

    async def close(self) -> None:
        self._closed = True

    async def stream(self, symbols: List[str]) -> AsyncIterator[MarketEvent]:
        wanted = set(symbols)
        last_ts: Optional[float] = None
        with open(self.path) as f:
            for line in f:
                if self._closed:
                    return
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("symbol") not in wanted:
                    continue

                event = _parse_event(self._venue, rec)
                if event is None:
                    continue

                if self.speed > 0 and last_ts is not None:
                    delta = (rec["ts"] - last_ts) / self.speed
                    if delta > 0:
                        await asyncio.sleep(delta)
                last_ts = rec["ts"]

                yield event


def _parse_event(venue: str, rec: dict) -> Optional[MarketEvent]:
    kind = rec.get("kind")
    ts = float(rec["ts"])
    sym = rec["symbol"]
    if kind == "book_snapshot":
        return BookFullSnapshot(
            ts=ts, venue=venue, symbol=sym,
            bids=tuple((float(p), float(s)) for p, s in rec.get("bids", [])),
            asks=tuple((float(p), float(s)) for p, s in rec.get("asks", [])),
        )
    if kind == "book_delta":
        return BookDelta(
            ts=ts, venue=venue, symbol=sym,
            side=rec["side"], price=float(rec["price"]), size=float(rec["size"]),
        )
    if kind == "trade":
        return TradeTick(
            ts=ts, venue=venue, symbol=sym,
            price=float(rec["price"]), size=float(rec["size"]),
            aggressor_side=rec["aggressor_side"],
        )
    if kind == "funding":
        return FundingTick(
            ts=ts, venue=venue, symbol=sym,
            rate_bps_per_8h=float(rec["rate_bps_per_8h"]),
            next_settlement_ts=rec.get("next_settlement_ts"),
        )
    return None


class RecorderVenue:
    """Wraps a real VenueClient and tees its event stream to a JSONL file
    for later replay / calibration.

    Not a VenueClient subclass — used by runner alongside a live client.
    """

    def __init__(self, output_path: str | Path):
        self.path = Path(output_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", buffering=1)

    def record(self, event: MarketEvent) -> None:
        rec = _event_to_record(event)
        if rec is None:
            return
        self._fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def close(self) -> None:
        self._fh.flush()
        self._fh.close()


def _event_to_record(e: MarketEvent) -> Optional[dict]:
    if isinstance(e, BookFullSnapshot):
        return {"kind": "book_snapshot", "ts": e.ts, "symbol": e.symbol,
                "bids": list(e.bids), "asks": list(e.asks)}
    if isinstance(e, BookDelta):
        return {"kind": "book_delta", "ts": e.ts, "symbol": e.symbol,
                "side": e.side, "price": e.price, "size": e.size}
    if isinstance(e, TradeTick):
        return {"kind": "trade", "ts": e.ts, "symbol": e.symbol,
                "price": e.price, "size": e.size,
                "aggressor_side": e.aggressor_side}
    if isinstance(e, FundingTick):
        return {"kind": "funding", "ts": e.ts, "symbol": e.symbol,
                "rate_bps_per_8h": e.rate_bps_per_8h,
                "next_settlement_ts": e.next_settlement_ts}
    return None
