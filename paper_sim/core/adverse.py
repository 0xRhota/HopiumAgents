"""Adverse selection tracker.

For every maker fill, we want to know: how did the mid price move in the
30 seconds after we got hit? If we bought at $79,000 and the mid drops to
$78,990 in the next 30s, we got adversely selected — the flow that hit us
was informed.

This module schedules the measurement and surfaces drift in bps as an
annotation on PaperFill.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple


@dataclass
class _PendingMeasurement:
    fill_id: str
    fill_ts: float
    measure_at_ts: float       # fill_ts + window_seconds
    fill_price: float
    side: str                   # "BUY" or "SELL"
    venue: str
    symbol: str


class AdverseSelectionTracker:
    """Manages T+window measurements for maker fills.

    Usage:
      - On every maker fill: register(fill_id, fill_ts, fill_price, side, venue, symbol)
      - On every book/trade tick: poll(current_ts, get_mid) — fires callbacks for
        any measurements whose window has elapsed.
      - The callback receives (fill_id, drift_bps).

    drift_bps semantics:
      - For BUY fills: drift_bps = (current_mid - fill_price) / fill_price * 10_000
        Positive drift = price went UP after we bought = good (favorable).
        Negative drift = price went DOWN after we bought = adverse.
      - For SELL fills: drift_bps = (fill_price - current_mid) / fill_price * 10_000
        Positive = price went DOWN after we sold = favorable.
        Negative = price went UP after we sold = adverse.

    Convention: positive bps = good for us, negative = we got picked off.
    """

    def __init__(self, window_seconds: float = 30.0):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = window_seconds
        self._pending: List[_PendingMeasurement] = []

    def register(self, fill_id: str, fill_ts: float, fill_price: float,
                 side: str, venue: str, symbol: str) -> None:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY/SELL, got {side!r}")
        self._pending.append(_PendingMeasurement(
            fill_id=fill_id,
            fill_ts=fill_ts,
            measure_at_ts=fill_ts + self.window_seconds,
            fill_price=fill_price,
            side=side,
            venue=venue,
            symbol=symbol,
        ))

    def poll(self, current_ts: float,
             get_mid: Callable[[str, str], Optional[float]]) -> List[Tuple[str, float]]:
        """Fire any due measurements. Returns list of (fill_id, drift_bps).

        get_mid(venue, symbol) returns the current mid for that market, or
        None if the book is empty.
        """
        results: List[Tuple[str, float]] = []
        still_pending: List[_PendingMeasurement] = []

        for m in self._pending:
            if current_ts < m.measure_at_ts:
                still_pending.append(m)
                continue

            current_mid = get_mid(m.venue, m.symbol)
            if current_mid is None or current_mid <= 0:
                # No data → drop measurement; we can't compute drift
                continue

            if m.side == "BUY":
                drift = (current_mid - m.fill_price) / m.fill_price * 10_000.0
            else:
                drift = (m.fill_price - current_mid) / m.fill_price * 10_000.0

            results.append((m.fill_id, drift))

        self._pending = still_pending
        return results

    def pending_count(self) -> int:
        return len(self._pending)
