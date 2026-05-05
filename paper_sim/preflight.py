"""Preflight check: validate a live venue connection delivers all expected
event types within a time budget.

Catches the class of bug we hit on 2026-05-05 — funding poll logged "OK" for
hours but the strategy's MarketState had 0 funding. The preflight runs a
fresh venue connection, counts events per type, and returns PASS/FAIL with
specifics. CI-friendly: exit 0 = ready to deploy, exit 1 = something broken.

Usage:
    python -m paper_sim.cli preflight --venue paradex --duration 90s
    python -m paper_sim.cli preflight --venue hyperliquid --duration 90s

Pass criteria (configurable):
  - At least 1 BookFullSnapshot OR BookDelta within the window
  - At least 1 TradeTick within the window
  - At least 1 FundingTick within the window
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List

from paper_sim.core.types import FundingTick, TradeTick
from paper_sim.venues.base import (
    BookDelta,
    BookFullSnapshot,
    MarketEvent,
    VenueClient,
)

logger = logging.getLogger(__name__)


@dataclass
class PreflightResult:
    venue: str
    duration_seconds: float
    counts: Counter = field(default_factory=Counter)
    first_seen_ts: Dict[str, float] = field(default_factory=dict)
    passed: bool = False
    failures: List[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"=== Preflight: {self.venue} ({self.duration_seconds:.0f}s) ==="]
        for et in ("BookFullSnapshot", "BookDelta", "TradeTick", "FundingTick"):
            n = self.counts.get(et, 0)
            first_at = self.first_seen_ts.get(et)
            first_str = f"first @ +{first_at:.1f}s" if first_at is not None else "NEVER"
            lines.append(f"  {et}: {n} events  ({first_str})")
        if self.passed:
            lines.append("  PASS")
        else:
            lines.append("  FAIL:")
            for f in self.failures:
                lines.append(f"    - {f}")
        return "\n".join(lines)


async def run_preflight(
    client: VenueClient,
    symbols: List[str],
    duration_seconds: float = 90.0,
) -> PreflightResult:
    """Subscribe to a venue, count events for `duration_seconds`,
    apply pass criteria, return result."""
    result = PreflightResult(venue=client.venue, duration_seconds=duration_seconds)
    start_ts: float | None = None

    await client.connect()
    try:
        async def collector():
            nonlocal start_ts
            async for event in client.stream(symbols):
                ts = event.ts if start_ts is None else event.ts
                if start_ts is None:
                    start_ts = ts
                et = type(event).__name__
                result.counts[et] += 1
                if et not in result.first_seen_ts:
                    result.first_seen_ts[et] = max(0.0, ts - start_ts)

        try:
            await asyncio.wait_for(collector(), timeout=duration_seconds)
        except asyncio.TimeoutError:
            pass  # expected — duration elapsed
    finally:
        await client.close()

    # Apply pass criteria
    book_events = result.counts.get("BookFullSnapshot", 0) + \
                  result.counts.get("BookDelta", 0)
    if book_events == 0:
        result.failures.append("no BookFullSnapshot or BookDelta events received")
    if result.counts.get("TradeTick", 0) == 0:
        result.failures.append("no TradeTick events received")
    if result.counts.get("FundingTick", 0) == 0:
        result.failures.append("no FundingTick events received")
    result.passed = not result.failures
    return result
