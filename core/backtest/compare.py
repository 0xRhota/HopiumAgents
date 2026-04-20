"""Sim vs Live PnL divergence checker — the trust gate.

A backtest is only trustworthy once compare_pnl(sim, live) passes over
the same window with the same strategy params. Paper-trade live for
3-7 days, then run this before anyone trusts the sim output.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from core.reconciliation.base import Fill


@dataclass
class ComparisonResult:
    sim_realized: float
    live_realized: float
    sim_fees: float
    live_fees: float
    sim_net: float
    live_net: float
    divergence_usd: float
    divergence_pct: float
    passed: bool
    notes: str


def compare_pnl(sim: List[Fill], live: List[Fill],
                tolerance_usd: float = 1.0,
                tolerance_pct: float = 0.05) -> ComparisonResult:
    sim_realized = sum(f.realized_pnl_usd or 0 for f in sim)
    sim_fees = sum(f.fee for f in sim)
    sim_net = sim_realized - sim_fees

    live_realized = sum(f.realized_pnl_usd or 0 for f in live)
    live_fees = sum(f.fee for f in live)
    live_net = live_realized - live_fees

    div_usd = abs(sim_net - live_net)
    div_pct = div_usd / max(1e-9, abs(live_net))

    notes = []
    if abs(sim_realized - live_realized) > tolerance_usd:
        notes.append(f"realized differs: sim={sim_realized:+.2f} live={live_realized:+.2f}")
    if abs(sim_fees - live_fees) > tolerance_usd:
        notes.append(f"fees differ: sim={sim_fees:+.4f} live={live_fees:+.4f}")
    if len(sim) != len(live):
        notes.append(f"trade count differs: sim={len(sim)} live={len(live)}")

    passed = div_usd <= tolerance_usd or div_pct <= tolerance_pct

    return ComparisonResult(
        sim_realized=sim_realized, live_realized=live_realized,
        sim_fees=sim_fees, live_fees=live_fees,
        sim_net=sim_net, live_net=live_net,
        divergence_usd=div_usd, divergence_pct=div_pct,
        passed=passed, notes="; ".join(notes) or "ok",
    )
