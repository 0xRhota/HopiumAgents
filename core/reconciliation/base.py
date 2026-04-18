"""Abstract base class and dataclasses for exchange reconciliation.

Design principle: exchange is source of truth. These types carry
data FROM the exchange to our internal state, never the reverse.

Sign conventions (critical — get these right):
- `fee`: positive = we paid fee (taker), negative = we received rebate (maker on some exchanges)
- `funding_paid`: positive = we paid funding, negative = we received funding
- `realized_pnl_usd`: gross of fees, raw price movement × size. Set only on CLOSE fills.
- `net_pnl_usd` (derived): realized_pnl - fee. The honest per-fill number.

All datetimes are tz-aware UTC. Never use naive datetimes anywhere.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Literal, Optional


Side = Literal["BUY", "SELL"]
PositionSide = Literal["LONG", "SHORT"]
FillKind = Literal["OPEN", "CLOSE"]


# ─── Fill ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Fill:
    """A single fill reported by the exchange.

    This is the atomic unit of truth. Every trade the bot cares about
    must produce one or more Fill records sourced from the exchange's
    fills/matches endpoint.
    """
    exchange: str
    symbol: str
    fill_id: str
    order_id: str
    ts: datetime
    side: Side
    size: float          # base asset units
    price: float         # exchange-reported fill price, not the limit price
    fee: float           # USD, signed (positive = paid, negative = rebate)
    is_maker: bool
    realized_pnl_usd: Optional[float]  # gross of fees; None on OPEN fills
    opens_or_closes: FillKind
    linked_entry_fill_id: Optional[str] = None  # populated only when reconstructing OPEN/CLOSE pairs (not yet wired)

    def __post_init__(self):
        if self.ts.tzinfo is None:
            raise ValueError(f"Fill.ts must be tz-aware, got naive {self.ts!r}")
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side: {self.side!r}")
        if self.opens_or_closes not in ("OPEN", "CLOSE"):
            raise ValueError(f"Invalid opens_or_closes: {self.opens_or_closes!r}")
        if self.opens_or_closes == "OPEN" and self.realized_pnl_usd is not None:
            raise ValueError("OPEN fills must have realized_pnl_usd=None")

    @property
    def notional_usd(self) -> float:
        """|size × price| — always positive."""
        return abs(self.size * self.price)

    @property
    def effective_cost(self) -> float:
        """Net cost of this fill (just the fee; rebate = negative cost)."""
        return self.fee

    @property
    def net_pnl_usd(self) -> Optional[float]:
        """realized_pnl - fee. None on OPEN fills."""
        if self.realized_pnl_usd is None:
            return None
        return self.realized_pnl_usd - self.fee

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Fill":
        d = dict(d)
        d["ts"] = datetime.fromisoformat(d["ts"])
        return cls(**d)


# ─── Position ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Position:
    """An open position as reported by the exchange."""
    exchange: str
    symbol: str
    side: PositionSide
    size: float
    entry_price: float
    unrealized_pnl: float
    funding_accrued: float  # signed; positive = we've paid; negative = we've received

    def __post_init__(self):
        if self.side not in ("LONG", "SHORT"):
            raise ValueError(f"Invalid side: {self.side!r}")

    @property
    def notional_usd(self) -> float:
        return abs(self.size * self.entry_price)


# ─── WindowPnL ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class WindowPnL:
    """Aggregate PnL for a time window, sourced from exchange."""
    exchange: str
    window_start: datetime
    window_end: datetime
    realized_pnl: float       # gross of fees
    fees_paid: float          # signed; positive = paid, negative = rebate
    funding_paid: float       # signed; positive = paid, negative = received
    trade_count: int

    @property
    def net_pnl(self) -> float:
        """realized - fees - funding.

        Signs: fees_paid and funding_paid are positive when WE paid them,
        so subtract both from realized.
        """
        return self.realized_pnl - self.fees_paid - self.funding_paid


# ─── ExchangeSnapshot ────────────────────────────────────────────────

@dataclass(frozen=True)
class ExchangeSnapshot:
    """Complete authoritative state from the exchange at a moment in time."""
    exchange: str
    ts: datetime
    equity: float
    positions: List[Position]
    new_fills: List[Fill]           # fills since the previous snapshot
    funding_paid_since: float       # aggregate funding paid between snapshots

    @property
    def total_notional(self) -> float:
        return sum(p.notional_usd for p in self.positions)

    @property
    def total_unrealized(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions)


# ─── Reconciler ABC ──────────────────────────────────────────────────

class Reconciler(ABC):
    """Interface every exchange reconciler must implement."""

    @property
    @abstractmethod
    def exchange(self) -> str:
        """Exchange name, lowercase (e.g. 'nado', 'paradex', 'hibachi')."""

    @abstractmethod
    async def snapshot(self, since: Optional[datetime] = None) -> ExchangeSnapshot:
        """Pull authoritative state from the exchange.

        Args:
            since: if provided, only return fills with ts >= since.
                   Used to pick up new fills between cycles.
        """

    @abstractmethod
    async def get_pnl_window(self, hours: int) -> WindowPnL:
        """Aggregate historical PnL over the past `hours`."""
