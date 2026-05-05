"""VenueClient ABC — the data boundary.

Implementations subscribe to L2 + trade tape + funding for a venue and
yield a stream of MarketEvents. The runner treats them as opaque data
sources; only this module knows about WebSockets, REST, etc.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, List, Union

from paper_sim.core.types import BookSnapshot, FundingTick, TradeTick


@dataclass(frozen=True)
class BookDelta:
    """Incremental book update."""
    ts: float
    venue: str
    symbol: str
    side: str           # "bid" or "ask"
    price: float
    size: float         # 0 = delete level


@dataclass(frozen=True)
class BookFullSnapshot:
    """Wholesale book replacement (initial WS sync)."""
    ts: float
    venue: str
    symbol: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


# Union of every event a venue can emit
MarketEvent = Union[BookDelta, BookFullSnapshot, TradeTick, FundingTick]


class VenueClient(ABC):
    """Streams market events for a fixed list of (venue, symbol) pairs."""

    @property
    @abstractmethod
    def venue(self) -> str: ...

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def stream(self, symbols: List[str]) -> AsyncIterator[MarketEvent]:
        """Subscribe and yield events forever (or until close())."""
        # Placeholder yield to satisfy AsyncIterator protocol; real impls override
        yield  # type: ignore[misc]
        raise NotImplementedError
