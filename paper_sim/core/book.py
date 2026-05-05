"""L2 orderbook maintenance.

Apply bid/ask deltas to keep an in-memory book current. Pure data structure;
no I/O. Snapshots are immutable BookSnapshots; the maintainer holds mutable
state internally and emits snapshots on demand.
"""
from __future__ import annotations

from typing import Dict

from paper_sim.core.types import BookSnapshot


class BookMaintainer:
    """Maintains a single (venue, symbol) L2 book.

    The book is held as two sorted dicts (bids: descending price → size,
    asks: ascending price → size). On each delta:
      - size > 0: set the level
      - size == 0: delete the level

    Snapshot the top-N levels via .snapshot(top_n=20).
    """

    def __init__(self, venue: str, symbol: str, *, max_levels: int = 100):
        self.venue = venue
        self.symbol = symbol
        self.max_levels = max_levels
        self._bids: Dict[float, float] = {}
        self._asks: Dict[float, float] = {}
        self._last_ts: float = 0.0

    def reset(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self._last_ts = 0.0

    def apply_snapshot(self, ts: float, bids: list[tuple[float, float]],
                       asks: list[tuple[float, float]]) -> None:
        """Replace the book wholesale (used for initial WS snapshot)."""
        self._bids = {p: s for p, s in bids if s > 0}
        self._asks = {p: s for p, s in asks if s > 0}
        self._last_ts = ts
        self._truncate()

    def apply_delta(self, ts: float, side: str, price: float, size: float) -> None:
        """Apply a single L2 delta. side ∈ {'bid','ask'}. size==0 deletes the level."""
        if side == "bid":
            book = self._bids
        elif side == "ask":
            book = self._asks
        else:
            raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")
        if size > 0:
            book[price] = size
        else:
            book.pop(price, None)
        self._last_ts = ts
        self._truncate()

    def _truncate(self) -> None:
        """Keep only top max_levels per side, by price proximity to mid."""
        if len(self._bids) > self.max_levels:
            keep = sorted(self._bids.keys(), reverse=True)[: self.max_levels]
            self._bids = {p: self._bids[p] for p in keep}
        if len(self._asks) > self.max_levels:
            keep = sorted(self._asks.keys())[: self.max_levels]
            self._asks = {p: self._asks[p] for p in keep}

    def snapshot(self, top_n: int = 20) -> BookSnapshot:
        bid_levels = sorted(self._bids.items(), key=lambda kv: -kv[0])[:top_n]
        ask_levels = sorted(self._asks.items(), key=lambda kv: kv[0])[:top_n]
        return BookSnapshot(
            ts=self._last_ts,
            venue=self.venue,
            symbol=self.symbol,
            bids=tuple(bid_levels),
            asks=tuple(ask_levels),
        )

    def size_at(self, side: str, price: float) -> float:
        """Resting size at exactly this price level. 0 if absent."""
        if side == "bid":
            return self._bids.get(price, 0.0)
        if side == "ask":
            return self._asks.get(price, 0.0)
        raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")

    def cumulative_size_at_or_better(self, side: str, price: float) -> float:
        """Total resting volume that would be hit before our order at `price`.

        For a BUY POST_ONLY at price P, "ahead of us" = bids at price >= P (better
        for the buyer = higher bids). For a SELL POST_ONLY at price P, "ahead" =
        asks at price <= P (better for seller = lower asks).
        """
        if side == "bid":
            return sum(s for p, s in self._bids.items() if p >= price)
        if side == "ask":
            return sum(s for p, s in self._asks.items() if p <= price)
        raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")
