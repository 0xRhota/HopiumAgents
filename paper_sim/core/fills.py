"""FillEngine — single source of truth for "did this order fill?"

Pipeline:
  1. Strategy emits IntendedOrder at ts_decision.
  2. LatencyInjector samples delay → ts_arrived = ts_decision + delay.
  3. FillEngine.place(order, ts_arrived):
       POST_ONLY:
         - if would cross book at ts_arrived → REJECT (returns Rejected)
         - else → register in QueuePositionTracker; return Resting(order_id)
       LIMIT:
         - if crosses → fill at level walk (taker fee); return Filled
         - else → register as maker; return Resting
       MARKET:
         - walk book at ts_arrived; return Filled (always taker fee)
  4. As trade ticks arrive, FillEngine.consume_trade fires resting orders that
     drain through the queue. Each yields a Filled.

All produced PaperFills carry latency, queue, fee, and funding metadata.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from paper_sim.core.book import BookMaintainer
from paper_sim.core.queue import QueuePositionTracker
from paper_sim.core.types import (
    BookSnapshot,
    IntendedOrder,
    PaperFill,
    TradeTick,
    VenueFees,
)


@dataclass(frozen=True)
class PlaceResult:
    """Outcome of placing an intended order."""
    fill: Optional[PaperFill] = None
    resting_order_id: Optional[str] = None
    rejected_reason: Optional[str] = None


FundingLookup = Callable[[str, str, float], float]
"""Signature: (venue, symbol, ts) -> funding_bps_per_8h. Bot must supply."""


class FillEngine:
    """Decides fills given live book state, queues, fees, funding.

    Stateless across calls except for the queue trackers it owns. Books and
    funding are read from external lookups passed in.
    """

    def __init__(
        self,
        fees: Dict[str, VenueFees],
        funding_lookup: FundingLookup,
    ):
        self.fees = fees
        self._funding = funding_lookup
        self._queues: Dict[Tuple[str, str], QueuePositionTracker] = {}

    # ---------- queue management ----------

    def get_queue(self, venue: str, symbol: str) -> QueuePositionTracker:
        key = (venue, symbol)
        if key not in self._queues:
            self._queues[key] = QueuePositionTracker(venue, symbol)
        return self._queues[key]

    def cancel_resting(self, venue: str, symbol: str, order_id: str) -> bool:
        return self.get_queue(venue, symbol).cancel(order_id)

    # ---------- public API ----------

    def place(
        self,
        order: IntendedOrder,
        book: BookSnapshot,
        ts_arrived: float,
    ) -> PlaceResult:
        """Process an order at ts_arrived using the supplied book snapshot."""
        if book.venue != order.venue or book.symbol != order.symbol:
            raise ValueError("book mismatches order")
        if order.venue not in self.fees:
            raise ValueError(f"no fee schedule for venue {order.venue}")

        if order.type == "MARKET":
            return self._fill_market(order, book, ts_arrived)
        if order.type == "POST_ONLY":
            return self._place_post_only(order, book, ts_arrived)
        if order.type == "LIMIT":
            return self._place_limit(order, book, ts_arrived)
        raise ValueError(f"unknown order type {order.type}")

    def consume_trade(self, trade: TradeTick) -> List[PaperFill]:
        """Apply a trade tick to resting maker orders. Returns the fills."""
        q = self.get_queue(trade.venue, trade.symbol)
        order_fills = q.consume_trade(trade.price, trade.size, trade.aggressor_side)
        results: List[PaperFill] = []
        for order_id, fill_size in order_fills:
            ctx = self._resting_context.get(order_id)
            if ctx is None:
                continue  # already consumed; defensive
            fee_bps = self.fees[ctx.venue].maker_bps
            fill = PaperFill(
                fill_id=f"{order_id}-{trade.ts:.6f}",
                ts_decision=ctx.ts_decision,
                ts_arrived=ctx.ts_arrived,
                ts_filled=trade.ts,
                venue=ctx.venue,
                symbol=ctx.symbol,
                side=ctx.side,
                price=ctx.price,
                size=fill_size,
                is_maker=True,
                fee_bps=fee_bps,
                fee_paid_usd=fee_bps / 10_000.0 * ctx.price * fill_size,
                funding_at_fill_bps=self._funding(ctx.venue, ctx.symbol, trade.ts),
                queue_ahead_at_arrival=ctx.queue_ahead_at_arrival,
                strategy_tag=ctx.strategy_tag,
            )
            results.append(fill)
            # Reduce remaining tracked size; if exhausted, drop context
            ctx.size_remaining -= fill_size
            if ctx.size_remaining <= 1e-12:
                self._resting_context.pop(order_id, None)
        return results

    # ---------- internals ----------

    @dataclass
    class _RestingContext:
        order_id: str
        venue: str
        symbol: str
        side: str
        price: float
        size_total: float
        size_remaining: float
        queue_ahead_at_arrival: float
        ts_decision: float
        ts_arrived: float
        strategy_tag: str

    @property
    def _resting_context(self) -> Dict[str, "_RestingContext"]:
        if not hasattr(self, "_rc"):
            self._rc: Dict[str, FillEngine._RestingContext] = {}
        return self._rc

    def _place_post_only(
        self, order: IntendedOrder, book: BookSnapshot, ts_arrived: float,
    ) -> PlaceResult:
        assert order.price is not None
        # Reject if would cross
        if order.side == "BUY":
            if book.best_ask is not None and order.price >= book.best_ask:
                return PlaceResult(rejected_reason="post_only_would_cross")
        else:
            if book.best_bid is not None and order.price <= book.best_bid:
                return PlaceResult(rejected_reason="post_only_would_cross")
        return self._register_resting(order, book, ts_arrived)

    def _place_limit(
        self, order: IntendedOrder, book: BookSnapshot, ts_arrived: float,
    ) -> PlaceResult:
        assert order.price is not None
        # Crosses → take liquidity at top of book
        if order.side == "BUY" and book.best_ask is not None and order.price >= book.best_ask:
            return self._fill_limit_cross(order, book, ts_arrived)
        if order.side == "SELL" and book.best_bid is not None and order.price <= book.best_bid:
            return self._fill_limit_cross(order, book, ts_arrived)
        return self._register_resting(order, book, ts_arrived)

    def _register_resting(
        self, order: IntendedOrder, book: BookSnapshot, ts_arrived: float,
    ) -> PlaceResult:
        assert order.price is not None
        order_id = order.client_id or str(uuid.uuid4())
        side = "bid" if order.side == "BUY" else "ask"
        # queue_ahead is sum of resting size at our level OR better
        # Sum from book snapshot
        if order.side == "BUY":
            queue_ahead = sum(s for p, s in book.bids if p >= order.price)
        else:
            queue_ahead = sum(s for p, s in book.asks if p <= order.price)

        q = self.get_queue(order.venue, order.symbol)
        q.register(
            order_id=order_id, side=order.side, price=order.price,
            size=order.size, queue_ahead=queue_ahead, ts_arrived=ts_arrived,
        )
        self._resting_context[order_id] = FillEngine._RestingContext(
            order_id=order_id, venue=order.venue, symbol=order.symbol,
            side=order.side, price=order.price,
            size_total=order.size, size_remaining=order.size,
            queue_ahead_at_arrival=queue_ahead,
            ts_decision=order.ts_decision, ts_arrived=ts_arrived,
            strategy_tag=order.strategy_tag,
        )
        return PlaceResult(resting_order_id=order_id)

    def _fill_limit_cross(
        self, order: IntendedOrder, book: BookSnapshot, ts_arrived: float,
    ) -> PlaceResult:
        # LIMIT that crosses → walk one or more levels, capped at limit price
        levels = book.asks if order.side == "BUY" else book.bids
        limit = order.price
        assert limit is not None
        usable = []
        for price, size in levels:
            if order.side == "BUY" and price > limit:
                break
            if order.side == "SELL" and price < limit:
                break
            usable.append((price, size))
        if not usable:
            return self._register_resting(order, book, ts_arrived)

        avg_price, filled_size = _walk_levels(usable, order.size)
        if filled_size <= 0:
            return self._register_resting(order, book, ts_arrived)

        fee_bps = self.fees[order.venue].taker_bps
        fill = PaperFill(
            fill_id=str(uuid.uuid4()),
            ts_decision=order.ts_decision,
            ts_arrived=ts_arrived,
            ts_filled=ts_arrived,
            venue=order.venue, symbol=order.symbol, side=order.side,
            price=avg_price, size=filled_size, is_maker=False,
            fee_bps=fee_bps,
            fee_paid_usd=fee_bps / 10_000.0 * avg_price * filled_size,
            funding_at_fill_bps=self._funding(order.venue, order.symbol, ts_arrived),
            queue_ahead_at_arrival=0.0,
            strategy_tag=order.strategy_tag,
        )
        return PlaceResult(fill=fill)

    def _fill_market(
        self, order: IntendedOrder, book: BookSnapshot, ts_arrived: float,
    ) -> PlaceResult:
        levels = book.asks if order.side == "BUY" else book.bids
        if not levels:
            return PlaceResult(rejected_reason="empty_book")
        avg_price, filled_size = _walk_levels(list(levels), order.size)
        if filled_size <= 0:
            return PlaceResult(rejected_reason="empty_book")

        fee_bps = self.fees[order.venue].taker_bps
        fill = PaperFill(
            fill_id=str(uuid.uuid4()),
            ts_decision=order.ts_decision,
            ts_arrived=ts_arrived,
            ts_filled=ts_arrived,
            venue=order.venue, symbol=order.symbol, side=order.side,
            price=avg_price, size=filled_size, is_maker=False,
            fee_bps=fee_bps,
            fee_paid_usd=fee_bps / 10_000.0 * avg_price * filled_size,
            funding_at_fill_bps=self._funding(order.venue, order.symbol, ts_arrived),
            queue_ahead_at_arrival=0.0,
            strategy_tag=order.strategy_tag,
        )
        return PlaceResult(fill=fill)


def _walk_levels(
    levels: List[Tuple[float, float]], size: float,
) -> Tuple[float, float]:
    """Walk price levels to fill `size`. Returns (vw_avg_price, filled_size).

    levels is iterated in order — caller is responsible for passing them
    sorted by execution priority (ascending for asks, descending for bids).
    """
    remaining = size
    notional = 0.0
    filled = 0.0
    for price, available in levels:
        if remaining <= 0:
            break
        take = min(remaining, available)
        notional += take * price
        filled += take
        remaining -= take
    if filled <= 0:
        return 0.0, 0.0
    return notional / filled, filled
