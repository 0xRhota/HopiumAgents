"""Strategy ABC.

Strategies are pure: they consume MarketState + PortfolioState and emit
a list of IntendedOrders. Side effects (placing, ledger writes) are the
runner's responsibility.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from paper_sim.core.types import (
    BookSnapshot,
    FundingTick,
    IntendedOrder,
    PortfolioSnapshot,
)


@dataclass
class MarketState:
    """All market info available to a strategy at a tick."""
    ts: float
    books: Dict[Tuple[str, str], BookSnapshot] = field(default_factory=dict)
    funding: Dict[Tuple[str, str], FundingTick] = field(default_factory=dict)
    # rolling 1m / 5m / 15m close history for the symbols the strategy cares about
    candles: Dict[Tuple[str, str, str], List[float]] = field(default_factory=dict)


class Strategy(ABC):
    """Pure decision function over (market, portfolio) → orders."""

    name: str = "base"

    @abstractmethod
    def venues(self) -> List[str]:
        """Which venues this strategy needs subscriptions on."""

    @abstractmethod
    def symbols(self, venue: str) -> List[str]:
        """Symbols on `venue` the strategy needs subscriptions for."""

    @abstractmethod
    def evaluate(
        self, market: MarketState, portfolio: PortfolioSnapshot
    ) -> List[IntendedOrder]:
        """Return any orders to place this tick. Empty list = no action."""
