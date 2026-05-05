"""Queue position tracker.

When a POST_ONLY order arrives at price level P:
  - Snapshot the resting volume already at price levels "ahead" of us
    (= would be filled before us when adversarial flow arrives)
  - Track cumulative volume that trades AT or THROUGH our level after arrival
  - Order fills when cumulative_through >= queue_ahead + our_partial_remaining

We model maker fills only — when a trade prints at price P with our side as the
PASSIVE side. If the aggressor is on our side (our SELL aggresses through asks),
we cannot be filled by that trade.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class _RestingOrder:
    order_id: str
    side: str        # "BUY" or "SELL"
    price: float
    size_remaining: float
    queue_ahead: float
    ts_arrived: float

    @property
    def fully_filled(self) -> bool:
        return self.size_remaining <= 1e-12


class QueuePositionTracker:
    """Track all resting POST_ONLY orders for a single (venue, symbol).

    Per-instance, not global. Runner instantiates one per (venue, symbol) pair.
    """

    def __init__(self, venue: str, symbol: str):
        self.venue = venue
        self.symbol = symbol
        self._orders: List[_RestingOrder] = []

    def register(self, order_id: str, side: str, price: float, size: float,
                 queue_ahead: float, ts_arrived: float) -> None:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
        self._orders.append(_RestingOrder(
            order_id=order_id, side=side, price=price, size_remaining=size,
            queue_ahead=queue_ahead, ts_arrived=ts_arrived,
        ))

    def cancel(self, order_id: str) -> bool:
        before = len(self._orders)
        self._orders = [o for o in self._orders if o.order_id != order_id]
        return len(self._orders) < before

    def open_orders(self) -> List[_RestingOrder]:
        return list(self._orders)

    def consume_trade(self, trade_price: float, trade_size: float,
                      aggressor_side: str) -> List[tuple[str, float]]:
        """Apply a trade tick to all matching resting orders.

        Returns a list of (order_id, fill_size) for any orders (partial or full)
        that filled as a result of this trade.

        Maker fill rules:
          - Trade at price P, aggressor was BUY (i.e. taker bought) → consumed asks at P.
            Our SELL POST_ONLY at price <= P could fill (asks below P were taken first).
          - Trade at price P, aggressor was SELL → consumed bids at P.
            Our BUY POST_ONLY at price >= P could fill.

        Within each eligible order: queue_ahead drains first, then our size fills.
        """
        if aggressor_side not in ("BUY", "SELL"):
            raise ValueError(f"aggressor_side must be 'BUY' or 'SELL'")

        # Which side of OUR orders can fill? Opposite of the aggressor.
        # Aggressor BUY consumes asks → our SELL orders fill.
        # Aggressor SELL consumes bids → our BUY orders fill.
        eligible_side = "SELL" if aggressor_side == "BUY" else "BUY"

        fills: List[tuple[str, float]] = []
        remaining_volume = trade_size

        # Match orders in priority order. For BUY orders eligible to fill,
        # higher-price orders fill first (better prices). For SELL, lower-price first.
        # Within same price, FIFO by ts_arrived.
        def priority_key(o: _RestingOrder):
            if o.side == "BUY":
                return (-o.price, o.ts_arrived)
            return (o.price, o.ts_arrived)

        # Filter to eligible orders whose price is reachable by this trade
        def is_reachable(o: _RestingOrder) -> bool:
            if o.side != eligible_side:
                return False
            if o.fully_filled:
                return False
            # BUY at P fills only if trade price <= P (someone aggressively sold at P, so
            # bids at P or higher get hit). SELL at P fills only if trade price >= P.
            if o.side == "BUY":
                return trade_price <= o.price
            return trade_price >= o.price

        candidates = sorted([o for o in self._orders if is_reachable(o)],
                            key=priority_key)

        for order in candidates:
            if remaining_volume <= 1e-12:
                break
            # Drain queue_ahead first
            if order.queue_ahead > 0:
                drain = min(order.queue_ahead, remaining_volume)
                order.queue_ahead -= drain
                remaining_volume -= drain
                if remaining_volume <= 1e-12:
                    break
            # Now fill our resting size
            fill = min(order.size_remaining, remaining_volume)
            order.size_remaining -= fill
            remaining_volume -= fill
            if fill > 0:
                fills.append((order.order_id, fill))

        # Drop fully-filled orders
        self._orders = [o for o in self._orders if not o.fully_filled]
        return fills

    def get_queue_ahead(self, order_id: str) -> Optional[float]:
        for o in self._orders:
            if o.order_id == order_id:
                return o.queue_ahead
        return None
