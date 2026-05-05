"""Tests for the four strategies. Focused on hard-cap enforcement and
correct order generation given clean market state."""
from __future__ import annotations

import pytest

from paper_sim.core.types import (
    BookSnapshot,
    FundingTick,
    Position,
    PortfolioSnapshot,
)
from paper_sim.strategies.account_a_tight_slow import (
    AccountATightSlow,
    AccountAConfig,
)
from paper_sim.strategies.account_b_funding_arb import (
    AccountBFundingArb,
    AccountBConfig,
)
from paper_sim.strategies.account_c_llm_scout import (
    AccountCLLMScout,
    AccountCConfig,
    LLMTradeIdea,
)
from paper_sim.strategies.account_d_hl_maker import (
    AccountDHLMaker,
    AccountDConfig,
)
from paper_sim.strategies.base import MarketState


def _empty_portfolio(equity: float = 5000.0) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        ts=0.0, account="test", equity=equity, cash=equity,
        positions=(), cumulative_fees_paid=0.0,
        cumulative_adverse_cost=0.0, cumulative_funding_paid=0.0,
    )


def _book(venue, symbol, ts=1.0, bid=100.0, ask=100.5):
    return BookSnapshot(
        ts=ts, venue=venue, symbol=symbol,
        bids=((bid, 1.0), (bid - 0.5, 2.0)),
        asks=((ask, 1.0), (ask + 0.5, 2.0)),
    )


# ─── Account A ─────────────────────────────────────────────────────────────

class TestAccountA:
    def test_no_orders_when_score_below_threshold(self):
        a = AccountATightSlow(AccountAConfig(score_min=4.5))
        # Empty market state → no candles → no signal
        ms = MarketState(ts=1.0, books={
            ("paradex", "BTC-USD-PERP"): _book("paradex", "BTC-USD-PERP")
        })
        orders = a.evaluate(ms, _empty_portfolio())
        assert orders == []

    def test_blocked_when_max_positions_reached(self):
        a = AccountATightSlow(AccountAConfig(max_positions=1))
        portfolio = PortfolioSnapshot(
            ts=0.0, account="test", equity=5000.0, cash=4000.0,
            positions=(Position(venue="paradex", symbol="BTC-USD-PERP",
                               side="BUY", size=0.001, entry_price=80000.0,
                               entry_ts=0.0),),
            cumulative_fees_paid=0.0, cumulative_adverse_cost=0.0,
            cumulative_funding_paid=0.0,
        )
        ms = MarketState(ts=1.0)
        assert a.evaluate(ms, portfolio) == []

    def test_per_symbol_weekly_cap(self):
        a = AccountATightSlow(AccountAConfig(max_trades_per_symbol_per_week=2))
        a.state.trade_count_by_symbol["BTC-USD-PERP"] = 2
        # Even with valid signal, cap blocks
        ms = MarketState(ts=1.0, books={
            ("paradex", "BTC-USD-PERP"): _book("paradex", "BTC-USD-PERP")
        })
        # Valid candle data for high score
        closes = [100.0 + i * 0.01 for i in range(40)]
        ms.candles[("paradex", "BTC-USD-PERP", "5m_close")] = closes
        ms.candles[("paradex", "BTC-USD-PERP", "5m_high")] = [c + 0.05 for c in closes]
        ms.candles[("paradex", "BTC-USD-PERP", "5m_low")] = [c - 0.05 for c in closes]
        ms.candles[("paradex", "BTC-USD-PERP", "5m_vol")] = [10.0] * 40
        orders = a.evaluate(ms, _empty_portfolio())
        # No order for BTC-USD-PERP since cap hit
        assert all(o.symbol != "BTC-USD-PERP" for o in orders)

    def test_venues_and_symbols(self):
        a = AccountATightSlow()
        assert a.venues() == ["paradex"]
        assert "BTC-USD-PERP" in a.symbols("paradex")
        assert a.symbols("hyperliquid") == []


# ─── Account B ─────────────────────────────────────────────────────────────

class TestAccountB:
    def test_no_action_when_funding_below_threshold(self):
        b = AccountBFundingArb(AccountBConfig(open_threshold_bps=3.0))
        ms = MarketState(ts=1.0)
        ms.funding[("paradex", "BTC-USD-PERP")] = FundingTick(
            ts=1.0, venue="paradex", symbol="BTC-USD-PERP", rate_bps_per_8h=1.0)
        ms.funding[("hyperliquid", "BTC")] = FundingTick(
            ts=1.0, venue="hyperliquid", symbol="BTC", rate_bps_per_8h=2.5)
        # spread = 1.5, below threshold
        assert b.evaluate(ms, _empty_portfolio()) == []

    def test_opens_pair_when_spread_exceeds_threshold(self):
        b = AccountBFundingArb(AccountBConfig(open_threshold_bps=3.0))
        ms = MarketState(ts=1.0)
        # paradex more negative → long paradex, short HL
        ms.funding[("paradex", "BTC-USD-PERP")] = FundingTick(
            ts=1.0, venue="paradex", symbol="BTC-USD-PERP", rate_bps_per_8h=-2.0)
        ms.funding[("hyperliquid", "BTC")] = FundingTick(
            ts=1.0, venue="hyperliquid", symbol="BTC", rate_bps_per_8h=4.0)
        ms.books[("paradex", "BTC-USD-PERP")] = _book("paradex", "BTC-USD-PERP",
                                                      bid=80000.0, ask=80001.0)
        ms.books[("hyperliquid", "BTC")] = _book("hyperliquid", "BTC",
                                                  bid=80000.0, ask=80001.0)
        orders = b.evaluate(ms, _empty_portfolio())
        assert len(orders) == 2
        sides = {(o.venue, o.side) for o in orders}
        assert ("paradex", "BUY") in sides
        assert ("hyperliquid", "SELL") in sides
        assert b.state.pair_open

    def test_doesnt_double_open(self):
        b = AccountBFundingArb()
        b.state.pair_open = True
        ms = MarketState(ts=1.0)
        ms.funding[("paradex", "BTC-USD-PERP")] = FundingTick(
            ts=1.0, venue="paradex", symbol="BTC-USD-PERP", rate_bps_per_8h=-10.0)
        ms.funding[("hyperliquid", "BTC")] = FundingTick(
            ts=1.0, venue="hyperliquid", symbol="BTC", rate_bps_per_8h=10.0)
        # Pair already open; spread huge but no portfolio positions to close → []
        assert b.evaluate(ms, _empty_portfolio()) == []


# ─── Account C ─────────────────────────────────────────────────────────────

class TestAccountC:
    def test_requires_two_llms(self):
        with pytest.raises(ValueError):
            AccountCLLMScout(llm_clients=[lambda x: []])

    def test_consensus_required_for_action(self):
        # LLM A says LONG BTC, LLM B says LONG ETH → no consensus → no orders
        a = lambda b: [LLMTradeIdea("BTC-USD-PERP", "LONG", 7, "thesis_a", 24)]
        b = lambda b: [LLMTradeIdea("ETH-USD-PERP", "LONG", 7, "thesis_b", 24)]
        c = AccountCLLMScout([a, b])

        ms = MarketState(ts=1.0)
        ms.books[("paradex", "BTC-USD-PERP")] = _book("paradex", "BTC-USD-PERP",
                                                     bid=80000.0, ask=80001.0)
        orders = c.evaluate(ms, _empty_portfolio())
        assert orders == []

    def test_consensus_produces_order(self):
        a = lambda b: [LLMTradeIdea("BTC-USD-PERP", "LONG", 8, "ta", 24)]
        b = lambda b: [LLMTradeIdea("BTC-USD-PERP", "LONG", 6, "tb", 24)]
        # Cold-start gate disabled so test doesn't need 10 fundings
        c = AccountCLLMScout([a, b],
                             AccountCConfig(min_funding_symbols_before_first_cycle=0))

        # ts must exceed cadence (4h default) so initial briefing fires
        ms = MarketState(ts=20_000.0)
        ms.books[("paradex", "BTC-USD-PERP")] = _book("paradex", "BTC-USD-PERP",
                                                     bid=80000.0, ask=80001.0)
        orders = c.evaluate(ms, _empty_portfolio())
        assert len(orders) == 1
        assert orders[0].symbol == "BTC-USD-PERP"
        assert orders[0].side == "BUY"

    def test_cadence_blocks_re_call(self):
        called = [0]
        def llm(b):
            called[0] += 1
            return [LLMTradeIdea("BTC-USD-PERP", "LONG", 8, "t", 24)]
        c = AccountCLLMScout([llm, llm], AccountCConfig(cadence_seconds=4 * 3600))

        ms = MarketState(ts=1000.0)
        ms.books[("paradex", "BTC-USD-PERP")] = _book("paradex", "BTC-USD-PERP",
                                                     bid=80000.0, ask=80001.0)
        c.evaluate(ms, _empty_portfolio())
        first_calls = called[0]

        # Second call 1h later — cadence blocks
        ms2 = MarketState(ts=1000.0 + 3600)
        ms2.books = ms.books
        c.evaluate(ms2, _empty_portfolio())
        assert called[0] == first_calls  # no new LLM call

    def test_weekly_cap(self):
        idea = lambda b: [LLMTradeIdea("BTC-USD-PERP", "LONG", 8, "t", 24)]
        c = AccountCLLMScout([idea, idea], AccountCConfig(max_trades_per_week=1))
        c.state.trades_this_week = 1

        ms = MarketState(ts=1.0)
        ms.books[("paradex", "BTC-USD-PERP")] = _book("paradex", "BTC-USD-PERP",
                                                     bid=80000.0, ask=80001.0)
        assert c.evaluate(ms, _empty_portfolio()) == []


# ─── Account D ─────────────────────────────────────────────────────────────

class TestAccountD:
    def test_quotes_both_sides(self):
        d = AccountDHLMaker()
        ms = MarketState(ts=1.0)
        ms.books[("hyperliquid", "BTC")] = _book("hyperliquid", "BTC",
                                                 bid=80000.0, ask=80001.0)
        orders = d.evaluate(ms, _empty_portfolio())
        sides = {o.side for o in orders}
        assert sides == {"BUY", "SELL"}

    def test_kill_switch_at_inventory_threshold(self):
        d = AccountDHLMaker(AccountDConfig(inventory_kill_threshold_usd=2000.0))
        portfolio = PortfolioSnapshot(
            ts=0.0, account="test", equity=5000.0, cash=2000.0,
            positions=(Position(venue="hyperliquid", symbol="BTC", side="BUY",
                               size=0.04, entry_price=80000.0, entry_ts=0.0),),
            cumulative_fees_paid=0.0, cumulative_adverse_cost=0.0,
            cumulative_funding_paid=0.0,
        )
        # 0.04 BTC × $80k = $3,200 long → exceeds $2k kill
        ms = MarketState(ts=1.0)
        ms.books[("hyperliquid", "BTC")] = _book("hyperliquid", "BTC",
                                                 bid=80000.0, ask=80001.0)
        orders = d.evaluate(ms, portfolio)
        assert len(orders) == 1
        assert orders[0].type == "MARKET"
        assert orders[0].side == "SELL"  # flatten long

    def test_cooldown_after_kill(self):
        d = AccountDHLMaker(AccountDConfig(cooldown_seconds=60.0))
        d.state.cooldown_until_ts = 1000.0
        ms = MarketState(ts=500.0)  # before cooldown ends
        ms.books[("hyperliquid", "BTC")] = _book("hyperliquid", "BTC")
        assert d.evaluate(ms, _empty_portfolio()) == []
