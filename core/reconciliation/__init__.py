"""Reconciliation-first PnL tracking.

Exchange = source of truth. Bot state = cache that must be verified every cycle.

See docs/RECONCILIATION_PLAN.md for the full architecture.
"""

from core.reconciliation.base import (
    ExchangeSnapshot,
    Fill,
    Position,
    Reconciler,
    WindowPnL,
)
from core.reconciliation.ledger import Ledger


def build_reconciler(exchange: str) -> Reconciler:
    """Factory. Keeps the two CLI scripts in sync."""
    exchange = exchange.lower()
    if exchange == "paradex":
        from core.reconciliation.paradex import ParadexReconciler
        return ParadexReconciler()
    if exchange == "nado":
        from core.reconciliation.nado import NadoReconciler
        return NadoReconciler()
    if exchange == "hibachi":
        from core.reconciliation.hibachi import HibachiReconciler
        return HibachiReconciler()
    raise ValueError(f"Unknown exchange: {exchange}")


__all__ = [
    "ExchangeSnapshot",
    "Fill",
    "Position",
    "Reconciler",
    "WindowPnL",
    "Ledger",
    "build_reconciler",
]
