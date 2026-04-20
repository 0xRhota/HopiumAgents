"""Momentum strategy adapter for the backtest runner.

Wraps core.strategies.momentum.engine.MomentumEngine so backtest and
live use the exact same scoring function — results are comparable.
"""
from __future__ import annotations

from typing import List, Optional

import pandas as pd

from core.strategies.momentum.engine import MomentumConfig, MomentumEngine


class BacktestMomentumStrategy:
    """Single-symbol wrapper. Multi-symbol is a future extension.

    Supports two pre-packaged configs plus a custom override:
    - preset="fast": current live config (score≥2.5, 80/40 bps fixed)
    - preset="slow": Paradex-style (score≥3.5, wider ATR exits, smaller size)
    """

    FAST_PRESET = {
        "score_min": 2.5, "tp_bps": 80.0, "sl_bps": 40.0, "size_pct": 20.0,
        "use_atr_exits": False,
    }
    SLOW_PRESET = {
        "score_min": 3.5, "size_pct": 10.0,
        "use_atr_exits": True, "tp_atr_mult": 3.0, "sl_atr_mult": 1.5,
        "tp_bps_floor": 150.0, "sl_bps_floor": 75.0,
        "tp_bps": 300.0, "sl_bps": 200.0,  # fallback if ATR missing
        "max_hold_minutes": 480.0,  # up to 8h holds
    }

    def __init__(self, symbol: str, preset: str = "fast", **overrides):
        self.symbol = symbol
        cfg = MomentumConfig()
        cfg.require_volume = False
        base = dict(self.FAST_PRESET if preset == "fast" else self.SLOW_PRESET)
        base.update(overrides)
        for k, v in base.items():
            setattr(cfg, k, v)
        self.cfg = cfg
        self.engine = MomentumEngine(cfg)
        self._last_trend: Optional[dict] = None

    def on_bar(self, ts, bar, portfolio, history: Optional[pd.DataFrame] = None) -> List[dict]:
        current = float(bar["close"])

        # Compute trend every bar — used for both exit (ATR) and entry
        if history is not None and len(history) >= 30:
            self._last_trend = self.engine.detect_trend(history)

        # Exit check
        if self.symbol in portfolio.positions:
            pos = portfolio.positions[self.symbol]
            entry = pos["entry_price"]
            # Effective TP/SL bps (same logic as engine.should_exit)
            if self.cfg.use_atr_exits and self._last_trend and self._last_trend.get("atr_bps", 0) > 0:
                atr = self._last_trend["atr_bps"]
                eff_tp = max(self.cfg.tp_bps_floor, atr * self.cfg.tp_atr_mult)
                eff_sl = max(self.cfg.sl_bps_floor, atr * self.cfg.sl_atr_mult)
            else:
                eff_tp = self.cfg.tp_bps
                eff_sl = self.cfg.sl_bps

            if pos["side"] == "LONG":
                bps_move = (current - entry) / entry * 10_000
            else:
                bps_move = (entry - current) / entry * 10_000
            if bps_move >= eff_tp or bps_move <= -eff_sl:
                return [{"action": "CLOSE", "symbol": self.symbol,
                         "limit_price": current, "post_only": False}]
            return []

        # Entry check
        if not self._last_trend or self._last_trend["direction"] == "NONE":
            return []
        trend = self._last_trend
        if trend["score"] < self.cfg.score_min:
            return []
        side = "LONG" if trend["direction"] == "LONG" else "SHORT"
        size_usd = portfolio.buying_power * (self.cfg.size_pct / 100.0)
        return [{"action": "OPEN", "symbol": self.symbol, "side": side,
                 "size": size_usd, "limit_price": current, "post_only": True}]
