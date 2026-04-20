"""Per-exchange fee + POST_ONLY rejection simulation.

Fee schedules verified live 2026-04-17/18. If an exchange changes its
published fee schedule, update the corresponding ExchangeSpec and re-run
the sim-vs-live trust gate (scripts/validate_strategy.py).

Sign convention: fee > 0 means we paid; fee < 0 means we received
rebate (Paradex maker).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True)
class ExchangeSpec:
    name: str
    maker_fee_bps: float          # signed: negative = rebate
    taker_fee_bps: float          # always positive
    supports_post_only: bool
    taker_slippage_ticks: int = 2
    tick_size: float = 0.0001     # default; runner can override per-symbol


NADO    = ExchangeSpec(name="nado",    maker_fee_bps=1.0,  taker_fee_bps=3.5,  supports_post_only=True)
PARADEX = ExchangeSpec(name="paradex", maker_fee_bps=-0.5, taker_fee_bps=2.0,  supports_post_only=True)
HIBACHI = ExchangeSpec(name="hibachi", maker_fee_bps=0.0,  taker_fee_bps=35.0, supports_post_only=False)


def simulate_order(spec: ExchangeSpec, side: Literal["BUY", "SELL"],
                   size: float, price: float, mid: float, half_spread: float,
                   post_only: bool) -> Optional[dict]:
    """Simulate an order at the given price/size. Returns fill dict or None.

    None = rejected (POST_ONLY crossed the book, or exchange doesn't support POST_ONLY).
    """
    if post_only:
        if not spec.supports_post_only:
            return None
        # POST_ONLY: limit must NOT cross the book
        if side == "BUY" and price >= mid + half_spread:
            return None
        if side == "SELL" and price <= mid - half_spread:
            return None
        notional = abs(size * price)
        fee = notional * (spec.maker_fee_bps / 10_000.0)
        return {"is_maker": True, "fill_price": price, "fee": fee}

    # Taker path
    slip_dir = 1 if side == "BUY" else -1
    fill_price = mid + slip_dir * (half_spread + spec.taker_slippage_ticks * spec.tick_size)
    notional = abs(size * fill_price)
    fee = notional * (spec.taker_fee_bps / 10_000.0)
    return {"is_maker": False, "fill_price": fill_price, "fee": fee}
