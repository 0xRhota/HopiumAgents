"""Divergence math for shadow-mode calibration. Pure functions, easy to test."""
from __future__ import annotations

from typing import List


def paper_vs_live_bps(live_price: float, paper_price: float) -> float:
    """Signed divergence in bps. Positive = paper higher than live."""
    if live_price <= 0:
        return 0.0
    return (paper_price - live_price) / live_price * 10_000.0


def median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def divergence_summary(divergences_bps: List[float]) -> dict:
    """Summary stats for a list of paper-vs-live bps measurements."""
    if not divergences_bps:
        return {"count": 0, "median_abs_bps": 0.0, "mean_abs_bps": 0.0,
                "p95_abs_bps": 0.0, "passes_2bp_gate": False}
    abs_div = sorted(abs(x) for x in divergences_bps)
    n = len(abs_div)
    p95 = abs_div[int(n * 0.95)] if n >= 20 else abs_div[-1]
    return {
        "count": n,
        "median_abs_bps": median(abs_div),
        "mean_abs_bps": sum(abs_div) / n,
        "p95_abs_bps": p95,
        "passes_2bp_gate": median(abs_div) < 2.0,
    }
