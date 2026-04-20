import pytest
import pandas as pd
from datetime import datetime, timezone

from core.backtest.runner import run_backtest
from core.backtest.exchange_sim import NADO
from core.reconciliation.base import Fill


class _StubStrategy:
    """Open at bar minute=30, close at bar minute=45."""
    def on_bar(self, ts, bar, portfolio, history=None):
        if ts.minute == 30:
            return [{"action": "OPEN", "symbol": "LIT-PERP", "side": "LONG",
                     "size": 100.0, "limit_price": bar["close"], "post_only": True}]
        if ts.minute == 45:
            return [{"action": "CLOSE", "symbol": "LIT-PERP",
                     "limit_price": bar["close"], "post_only": False}]
        return []


def _bars():
    idx = pd.date_range("2026-04-18 12:15", periods=8, freq="5min", tz="UTC")
    closes = [1.00, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07]
    return pd.DataFrame({"close": closes, "high": closes, "low": closes,
                         "open": closes, "volume": [100]*8}, index=idx)


def test_runner_emits_fill_records_in_ledger_schema():
    fills = run_backtest(strategy=_StubStrategy(), bars=_bars(), exchange=NADO,
                         starting_equity=100.0, leverage=10.0)
    assert all(isinstance(f, Fill) for f in fills)
    assert len(fills) == 2
    assert fills[0].opens_or_closes == "OPEN"
    assert fills[1].opens_or_closes == "CLOSE"
    assert fills[0].exchange == "nado"


def test_runner_skips_when_post_only_rejects():
    class _BadEntry:
        def on_bar(self, ts, bar, portfolio, history=None):
            if ts.minute == 30:
                return [{"action": "OPEN", "symbol": "X", "side": "LONG",
                         "size": 100.0, "limit_price": bar["close"] * 2,
                         "post_only": True}]
            return []
    fills = run_backtest(strategy=_BadEntry(), bars=_bars(), exchange=NADO,
                         starting_equity=100.0, leverage=10.0)
    assert len(fills) == 0


def test_runner_populates_realized_pnl_on_close_fills():
    fills = run_backtest(strategy=_StubStrategy(), bars=_bars(), exchange=NADO,
                         starting_equity=100.0, leverage=10.0)
    close_fill = next(f for f in fills if f.opens_or_closes == "CLOSE")
    assert close_fill.realized_pnl_usd is not None
    # Entry (maker post-only accepted) at $1.01, exit (taker) near $1.04
    assert close_fill.realized_pnl_usd > 0


def test_runner_skips_close_when_no_position():
    class _CloseWithoutOpen:
        def on_bar(self, ts, bar, portfolio, history=None):
            if ts.minute == 30:
                return [{"action": "CLOSE", "symbol": "X",
                         "limit_price": bar["close"], "post_only": False}]
            return []
    fills = run_backtest(strategy=_CloseWithoutOpen(), bars=_bars(), exchange=NADO,
                         starting_equity=100.0, leverage=10.0)
    assert len(fills) == 0


def test_runner_fills_use_bar_timestamp():
    fills = run_backtest(strategy=_StubStrategy(), bars=_bars(), exchange=NADO,
                         starting_equity=100.0, leverage=10.0)
    for f in fills:
        assert f.ts.tzinfo is not None
