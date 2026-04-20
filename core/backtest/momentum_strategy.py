"""Momentum strategy adapter for the backtest runner.

Wraps core.strategies.momentum.engine.MomentumEngine so backtest and
live use the exact same scoring function — results are comparable.
"""
from __future__ import annotations

from typing import List, Optional

import pandas as pd

from core.strategies.momentum.engine import MomentumConfig, MomentumEngine


class BacktestMomentumStrategy:
    """Single-symbol wrapper. Multi-symbol is a future extension."""

    def __init__(self, symbol: str, score_min: float = 2.5,
                 tp_bps: float = 80.0, sl_bps: float = 40.0,
                 size_pct: float = 20.0):
        self.symbol = symbol
        cfg = MomentumConfig()
        cfg.score_min = score_min
        cfg.tp_bps = tp_bps
        cfg.sl_bps = sl_bps
        cfg.size_pct = size_pct
        cfg.require_volume = False  # matches live bot default
        self.cfg = cfg
        self.engine = MomentumEngine(cfg)

    def on_bar(self, ts, bar, portfolio, history: Optional[pd.DataFrame] = None) -> List[dict]:
        current = float(bar["close"])

        # Exit check — bps move from entry vs TP/SL
        if self.symbol in portfolio.positions:
            pos = portfolio.positions[self.symbol]
            entry = pos["entry_price"]
            if pos["side"] == "LONG":
                bps_move = (current - entry) / entry * 10_000
            else:
                bps_move = (entry - current) / entry * 10_000
            if bps_move >= self.cfg.tp_bps or bps_move <= -self.cfg.sl_bps:
                return [{"action": "CLOSE", "symbol": self.symbol,
                         "limit_price": current, "post_only": False}]
            return []

        # Entry check — need enough history for detect_trend
        if history is None or len(history) < 30:
            return []
        trend = self.engine.detect_trend(history)
        if trend["direction"] == "NONE":
            return []
        side = "LONG" if trend["direction"] == "LONG" else "SHORT"
        size_usd = portfolio.buying_power * (self.cfg.size_pct / 100.0)
        return [{"action": "OPEN", "symbol": self.symbol, "side": side,
                 "size": size_usd, "limit_price": current, "post_only": True}]
