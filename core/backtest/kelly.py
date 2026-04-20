"""Kelly fraction from CLOSE Fill records.

Standard half-Kelly recommendation: multiply result by 0.5 before use.
"""
from __future__ import annotations

from typing import List

from core.reconciliation.base import Fill


def kelly_fraction(fills: List[Fill]) -> float:
    closes = [f for f in fills
              if f.opens_or_closes == "CLOSE" and f.realized_pnl_usd is not None]
    wins = [f.realized_pnl_usd for f in closes if f.realized_pnl_usd > 0]
    losses = [-f.realized_pnl_usd for f in closes if f.realized_pnl_usd < 0]
    if not wins or not losses:
        return 0.0
    p = len(wins) / len(closes)
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)
    b = avg_win / avg_loss
    f = (p * b - (1 - p)) / b
    return max(0.0, min(1.0, f))
