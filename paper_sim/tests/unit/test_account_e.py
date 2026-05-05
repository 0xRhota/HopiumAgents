"""Tests for Account E — ETH/BTC pair stat arb."""
from __future__ import annotations

import pytest

from paper_sim.core.types import (
    BookSnapshot,
    Position,
    PortfolioSnapshot,
)
from paper_sim.strategies.account_e_eth_btc_pair import (
    AccountEConfig,
    AccountEEthBtcPair,
)
from paper_sim.strategies.base import MarketState


def _portfolio(positions=()):
    return PortfolioSnapshot(
        ts=0.0, account="E", equity=5000.0, cash=4000.0,
        positions=tuple(positions),
        cumulative_fees_paid=0.0, cumulative_adverse_cost=0.0,
        cumulative_funding_paid=0.0,
    )


def _ms_with_ratio(ratios, btc_anchor=80000.0, ts=10000.0):
    """Build MarketState where the LAST ratio in `ratios` is current.

    ratios: list of ETH/BTC ratio values (length = candle history)
    Last value implies current mid prices via btc_anchor.
    """
    ms = MarketState(ts=ts)
    btc_closes = [btc_anchor] * len(ratios)
    eth_closes = [r * btc_anchor for r in ratios]
    ms.candles[("paradex", "BTC-USD-PERP", "5m_close")] = btc_closes
    ms.candles[("paradex", "ETH-USD-PERP", "5m_close")] = eth_closes
    # Build books at the last bar's prices
    last_btc = btc_closes[-1]
    last_eth = eth_closes[-1]
    ms.books[("paradex", "BTC-USD-PERP")] = BookSnapshot(
        ts=ts, venue="paradex", symbol="BTC-USD-PERP",
        bids=((last_btc - 1, 1.0),), asks=((last_btc + 1, 1.0),),
    )
    ms.books[("paradex", "ETH-USD-PERP")] = BookSnapshot(
        ts=ts, venue="paradex", symbol="ETH-USD-PERP",
        bids=((last_eth - 0.5, 5.0),), asks=((last_eth + 0.5, 5.0),),
    )
    return ms


def test_no_action_with_short_history():
    s = AccountEEthBtcPair(AccountEConfig(min_history_bars=60))
    ms = _ms_with_ratio([0.04] * 30)  # only 30 bars, < min_history
    assert s.evaluate(ms, _portfolio()) == []


def test_no_action_when_z_below_threshold():
    # Stable ratio at 0.04, then current sits exactly on mean → z=0
    ratios = [0.04] * 100
    s = AccountEEthBtcPair(AccountEConfig(min_history_bars=60, z_open_threshold=2.0))
    ms = _ms_with_ratio(ratios)
    assert s.evaluate(ms, _portfolio()) == []


def test_opens_pair_when_z_above_threshold_eth_overpriced():
    # Mostly 0.04, last bar spikes to 0.05 → ETH overpriced vs BTC
    ratios = [0.04] * 99 + [0.05]
    s = AccountEEthBtcPair(AccountEConfig(min_history_bars=60, z_open_threshold=2.0))
    ms = _ms_with_ratio(ratios)
    orders = s.evaluate(ms, _portfolio())
    assert len(orders) == 2
    eth_order = next(o for o in orders if o.symbol == "ETH-USD-PERP")
    btc_order = next(o for o in orders if o.symbol == "BTC-USD-PERP")
    assert eth_order.side == "SELL"  # short overpriced
    assert btc_order.side == "BUY"   # long underpriced
    assert s.state.pair_open is True
    assert s.state.pair_direction == "ETH_SHORT_BTC_LONG"


def test_opens_pair_when_z_below_negative_threshold_eth_underpriced():
    ratios = [0.04] * 99 + [0.03]   # last bar drops → ETH underpriced
    s = AccountEEthBtcPair(AccountEConfig(min_history_bars=60, z_open_threshold=2.0))
    ms = _ms_with_ratio(ratios)
    orders = s.evaluate(ms, _portfolio())
    eth_order = next(o for o in orders if o.symbol == "ETH-USD-PERP")
    btc_order = next(o for o in orders if o.symbol == "BTC-USD-PERP")
    assert eth_order.side == "BUY"
    assert btc_order.side == "SELL"
    assert s.state.pair_direction == "ETH_LONG_BTC_SHORT"


def test_closes_pair_when_z_returns_to_mean():
    s = AccountEEthBtcPair(AccountEConfig(min_history_bars=60))
    s.state.pair_open = True
    s.state.pair_direction = "ETH_SHORT_BTC_LONG"
    # Current ratio sits on mean → z=0 → close
    ratios = [0.04] * 100
    ms = _ms_with_ratio(ratios)
    portfolio = _portfolio(positions=[
        Position(venue="paradex", symbol="ETH-USD-PERP", side="SELL",
                 size=0.5, entry_price=3500.0, entry_ts=0.0),
        Position(venue="paradex", symbol="BTC-USD-PERP", side="BUY",
                 size=0.025, entry_price=80000.0, entry_ts=0.0),
    ])
    orders = s.evaluate(ms, portfolio)
    assert len(orders) == 2
    # Both must be reduce_only and reverse the open side
    sides = {(o.symbol, o.side) for o in orders}
    assert ("ETH-USD-PERP", "BUY") in sides
    assert ("BTC-USD-PERP", "SELL") in sides
    assert all(o.reduce_only for o in orders)
    assert s.state.pair_open is False


def test_throttle_prevents_back_to_back_attempts():
    s = AccountEEthBtcPair(AccountEConfig(min_history_bars=60,
                                          min_seconds_between_attempts=300))
    s.state.last_attempt_ts = 9990.0
    ms = _ms_with_ratio([0.04] * 99 + [0.05], ts=10000.0)
    # 10s elapsed < 300 throttle → no action even though z > threshold
    assert s.evaluate(ms, _portfolio()) == []


def test_emergency_stop_at_extreme_z():
    s = AccountEEthBtcPair(AccountEConfig(min_history_bars=60,
                                          z_stop_threshold=4.0))
    s.state.pair_open = True
    s.state.pair_direction = "ETH_SHORT_BTC_LONG"
    # 99 bars at 0.04, last spikes 5x → z huge
    ratios = [0.04] * 99 + [0.20]
    ms = _ms_with_ratio(ratios)
    portfolio = _portfolio(positions=[
        Position(venue="paradex", symbol="ETH-USD-PERP", side="SELL",
                 size=0.5, entry_price=3500.0, entry_ts=0.0),
    ])
    orders = s.evaluate(ms, portfolio)
    # Emergency close fires
    assert any(o.reduce_only for o in orders)
    assert s.state.pair_open is False


def test_venues_and_symbols():
    s = AccountEEthBtcPair()
    assert s.venues() == ["paradex"]
    syms = s.symbols("paradex")
    assert "BTC-USD-PERP" in syms
    assert "ETH-USD-PERP" in syms
    assert s.symbols("hyperliquid") == []
