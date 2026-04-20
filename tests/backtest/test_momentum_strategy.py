import pytest
import pandas as pd
from datetime import datetime, timezone

from core.backtest.momentum_strategy import BacktestMomentumStrategy
from core.backtest.portfolio import Portfolio


def _bars(closes):
    idx = pd.date_range("2026-04-18", periods=len(closes), freq="15min", tz="UTC")
    return pd.DataFrame({"close": closes, "high": closes, "low": closes,
                         "open": closes, "volume": [1000]*len(closes)}, index=idx)


def test_strategy_emits_no_action_on_flat_data():
    s = BacktestMomentumStrategy(symbol="LIT-PERP", score_min=2.5)
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    bars = _bars([1.0] * 60)
    actions = s.on_bar(bars.index[-1], bars.iloc[-1], p, history=bars)
    assert actions == []


def test_strategy_emits_close_when_tp_hit():
    s = BacktestMomentumStrategy(symbol="LIT-PERP", tp_bps=80, sl_bps=40)
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("LIT-PERP", "LONG", 100.0, 1.0,
                    fee=0.01, ts=datetime(2026, 4, 18, tzinfo=timezone.utc),
                    is_maker=True)
    bars = _bars([1.0, 1.005, 1.009])
    actions = s.on_bar(bars.index[-1], bars.iloc[-1], p, history=bars)
    assert len(actions) == 1
    assert actions[0]["action"] == "CLOSE"


def test_strategy_emits_close_when_sl_hit():
    s = BacktestMomentumStrategy(symbol="LIT-PERP", tp_bps=80, sl_bps=40)
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("LIT-PERP", "LONG", 100.0, 1.0,
                    fee=0.01, ts=datetime(2026, 4, 18, tzinfo=timezone.utc),
                    is_maker=True)
    bars = _bars([1.0, 0.998, 0.995])  # -50 bps
    actions = s.on_bar(bars.index[-1], bars.iloc[-1], p, history=bars)
    assert len(actions) == 1
    assert actions[0]["action"] == "CLOSE"


def test_strategy_short_tp_triggers_on_price_drop():
    s = BacktestMomentumStrategy(symbol="X", tp_bps=80, sl_bps=40)
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("X", "SHORT", 100.0, 1.0,
                    fee=0.01, ts=datetime(2026, 4, 18, tzinfo=timezone.utc),
                    is_maker=True)
    bars = _bars([1.0, 0.995, 0.990])  # down 100 bps → profit for SHORT
    actions = s.on_bar(bars.index[-1], bars.iloc[-1], p, history=bars)
    assert len(actions) == 1
    assert actions[0]["action"] == "CLOSE"


def test_strategy_requires_history_for_signal():
    """Without enough bars, detect_trend can't score. Skip entries."""
    s = BacktestMomentumStrategy(symbol="LIT-PERP", score_min=2.5)
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    bars = _bars([1.0])
    actions = s.on_bar(bars.index[-1], bars.iloc[-1], p, history=bars)
    assert actions == []
