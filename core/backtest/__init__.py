"""Perp-DEX backtest simulator.

Emits ledger-shaped Fill records under realistic fee + POST_ONLY rejection
models so backtest PnL is directly comparable to live ledger PnL.

See docs/plans/2026-04-18-backtest-simulator.md for the full plan.
"""

from core.backtest.exchange_sim import (
    ExchangeSpec, NADO, HIBACHI, PARADEX, simulate_order,
)
from core.backtest.portfolio import Portfolio
from core.backtest.runner import run_backtest
from core.backtest.momentum_strategy import BacktestMomentumStrategy
from core.backtest.walk_forward import walk_forward, WindowResult
from core.backtest.grid_search import grid_search
from core.backtest.kelly import kelly_fraction
from core.backtest.compare import compare_pnl, ComparisonResult

__all__ = [
    "ExchangeSpec", "NADO", "HIBACHI", "PARADEX", "simulate_order",
    "Portfolio", "run_backtest", "BacktestMomentumStrategy",
    "walk_forward", "WindowResult",
    "grid_search", "kelly_fraction",
    "compare_pnl", "ComparisonResult",
]
