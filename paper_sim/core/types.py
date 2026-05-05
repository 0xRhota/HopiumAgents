"""Frozen dataclass types for the paper trading sim.

All sim state flows through these types. Immutability is the invariant —
everything else is consequences of that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

Side = Literal["BUY", "SELL"]
OrderType = Literal["POST_ONLY", "LIMIT", "MARKET"]
PriceLevel = tuple[float, float]  # (price, size)


@dataclass(frozen=True)
class BookSnapshot:
    """L2 orderbook snapshot. Bids descending, asks ascending."""
    ts: float
    venue: str
    symbol: str
    bids: tuple[PriceLevel, ...]
    asks: tuple[PriceLevel, ...]

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    @property
    def spread_bps(self) -> Optional[float]:
        m = self.mid
        bb, ba = self.best_bid, self.best_ask
        if not m or m == 0 or bb is None or ba is None:
            return None
        return (ba - bb) / m * 10_000.0


@dataclass(frozen=True)
class TradeTick:
    """A trade printed to the tape."""
    ts: float
    venue: str
    symbol: str
    price: float
    size: float
    aggressor_side: Side  # which side was the taker


@dataclass(frozen=True)
class FundingTick:
    """Latest funding rate for a market.

    Convention: rate_bps_per_8h. Positive = longs pay shorts.
    """
    ts: float
    venue: str
    symbol: str
    rate_bps_per_8h: float
    next_settlement_ts: Optional[float] = None


@dataclass(frozen=True)
class IntendedOrder:
    """An order a strategy WANTS to place. Not yet been delayed by latency."""
    ts_decision: float
    venue: str
    symbol: str
    side: Side
    type: OrderType
    size: float
    price: Optional[float] = None  # required for LIMIT and POST_ONLY
    strategy_tag: str = ""
    reduce_only: bool = False
    client_id: str = ""

    def __post_init__(self) -> None:
        if self.type in ("LIMIT", "POST_ONLY") and self.price is None:
            raise ValueError(f"{self.type} requires a price")
        if self.size <= 0:
            raise ValueError(f"size must be positive, got {self.size}")


@dataclass(frozen=True)
class PaperFill:
    """A simulated fill, with all the truth we can capture about it.

    Immutable except for adverse_drift_bps_t30 which is filled in by
    AdverseSelectionTracker — handled via dataclasses.replace, not mutation.
    """
    fill_id: str
    ts_decision: float
    ts_arrived: float       # ts_decision + sampled latency
    ts_filled: float
    venue: str
    symbol: str
    side: Side
    price: float
    size: float
    is_maker: bool
    fee_bps: float
    fee_paid_usd: float
    funding_at_fill_bps: float
    queue_ahead_at_arrival: float
    strategy_tag: str = ""
    adverse_drift_bps_t30: Optional[float] = None


@dataclass(frozen=True)
class Position:
    """Open position state for a (venue, symbol)."""
    venue: str
    symbol: str
    side: Side
    size: float
    entry_price: float
    entry_ts: float


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Account state at a point in time."""
    ts: float
    account: str
    equity: float
    cash: float
    positions: tuple[Position, ...]
    cumulative_fees_paid: float
    cumulative_adverse_cost: float
    cumulative_funding_paid: float


@dataclass(frozen=True)
class VenueFees:
    """Static fee schedule for a venue. bps as positive = we pay,
    negative = we receive (rebate)."""
    venue: str
    maker_bps: float
    taker_bps: float
