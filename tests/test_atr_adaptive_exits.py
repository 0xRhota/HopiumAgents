"""ATR-based adaptive TP/SL. Replaces fixed 80/40 bps with vol-scaled exits.

Motivation: SUI (tight spread, high-freq MM) gets chopped by 40 bps SL while
TAO (wider spread) exits cleanly. Adapt the exit distance to the asset's
own volatility instead of blacklisting symbols.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import pytest
import time

from core.strategies.momentum.engine import MomentumConfig, MomentumEngine


def _bars(closes, volume=1000):
    idx = pd.date_range("2026-04-20", periods=len(closes), freq="15min", tz="UTC")
    n = len(closes)
    return pd.DataFrame({
        "open": closes, "close": closes,
        "high": [c * 1.001 for c in closes], "low": [c * 0.999 for c in closes],
        "volume": [volume] * n,
    }, index=idx)


def test_detect_trend_includes_atr_bps():
    """Every trend dict from a valid df must expose atr_bps."""
    eng = MomentumEngine(MomentumConfig())
    df = _bars([1.0 + 0.001 * i for i in range(40)])
    trend = eng.detect_trend(df)
    assert "atr_bps" in trend
    assert trend["atr_bps"] > 0


def test_atr_scales_with_realized_volatility():
    """High-vol asset → higher atr_bps. Low-vol → lower."""
    eng = MomentumEngine(MomentumConfig())
    calm = _bars([1.0 + 0.0001 * i for i in range(40)])
    vol = _bars([1.0 + 0.01 * ((i % 2) - 0.5) for i in range(40)])
    atr_calm = eng.detect_trend(calm)["atr_bps"]
    atr_vol = eng.detect_trend(vol)["atr_bps"]
    assert atr_vol > atr_calm * 2


def test_should_exit_uses_atr_when_enabled():
    """With use_atr_exits=True, TP/SL scale from the trend dict's atr_bps."""
    cfg = MomentumConfig()
    cfg.use_atr_exits = True
    cfg.tp_atr_mult = 2.0
    cfg.sl_atr_mult = 1.0
    cfg.tp_bps_floor = 30.0  # floor if ATR too small
    cfg.sl_bps_floor = 15.0
    eng = MomentumEngine(cfg)

    entry = 100.0
    now = time.time()
    # ATR = 50 bps → TP = 100 bps (2x), SL = 50 bps (1x)
    trend = {"direction": "LONG", "score": 2.5, "atr_bps": 50.0}

    # Price up 101 bps → TP hits
    assert eng.should_exit(entry * 1.0101, entry, "LONG", now, trend) == "TP"
    # Price up 99 bps → no TP yet
    assert eng.should_exit(entry * 1.0099, entry, "LONG", now, trend) != "TP"
    # Price down 51 bps → SL hits
    assert eng.should_exit(entry * 0.9949, entry, "LONG", now, trend) == "SL"
    # Price down 49 bps → no SL yet
    assert eng.should_exit(entry * 0.9951, entry, "LONG", now, trend) != "SL"


def test_atr_exits_respect_floor():
    """If ATR tiny, floor kicks in so we don't use 2-bps exits."""
    cfg = MomentumConfig()
    cfg.use_atr_exits = True
    cfg.tp_atr_mult = 2.0
    cfg.sl_atr_mult = 1.0
    cfg.tp_bps_floor = 30.0
    cfg.sl_bps_floor = 15.0
    eng = MomentumEngine(cfg)

    entry = 100.0
    now = time.time()
    trend = {"direction": "LONG", "score": 2.5, "atr_bps": 2.0}  # way below floor

    # With floor=30, TP at 30 bps not 4 bps
    assert eng.should_exit(entry * 1.003, entry, "LONG", now, trend) == "TP"
    assert eng.should_exit(entry * 1.0025, entry, "LONG", now, trend) != "TP"


def test_fixed_tp_sl_still_works_when_atr_disabled():
    """Default behavior unchanged: use_atr_exits=False uses config.tp_bps."""
    cfg = MomentumConfig()
    # cfg.use_atr_exits defaults to False
    cfg.tp_bps = 80.0
    cfg.sl_bps = 40.0
    eng = MomentumEngine(cfg)

    entry = 100.0
    now = time.time()
    trend = {"direction": "LONG", "score": 2.5, "atr_bps": 5.0}

    # Fixed 80 bps TP
    assert eng.should_exit(entry * 1.009, entry, "LONG", now, trend) == "TP"
    # Fixed 40 bps SL
    assert eng.should_exit(entry * 0.995, entry, "LONG", now, trend) == "SL"
