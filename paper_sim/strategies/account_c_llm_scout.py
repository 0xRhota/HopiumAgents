"""Account C — LLM market-awareness scout, multi-asset.

Hypothesis: an LLM consuming a structured market briefing (funding extremes,
OI deltas, BTC regime, top movers, recent liquidations) every 4h identifies
trades a price-only scanner misses. Two-LLM consensus filter prevents the
runaway-trade-count failure mode observed in Alpha Arena.

Constraints:
  - ≤10 trades/week
  - ≥4h between any 2 entries
  - ≤3 concurrent positions
  - Position size: 0.25 × (conviction/10) × equity
  - Exit on LLM "thesis broken" signal OR ATR stop OR 7-day hold
  - Two LLMs (Claude Opus + DeepSeek-V3) must agree on direction + symbol

The LLM client is supplied at construction so tests can inject a mock.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from paper_sim.core.types import IntendedOrder, PortfolioSnapshot
from paper_sim.strategies.base import MarketState, Strategy


@dataclass
class LLMTradeIdea:
    symbol: str
    direction: str          # "LONG" or "SHORT"
    conviction: int         # 1..10
    thesis: str
    time_horizon_hours: float


# Type alias: client(briefing_dict) -> list of ideas (or [])
LLMClient = Callable[[dict], List[LLMTradeIdea]]


@dataclass
class AccountCConfig:
    venue: str = "paradex"
    universe: List[str] = field(default_factory=lambda: [
        "BTC-USD-PERP", "ETH-USD-PERP", "SOL-USD-PERP", "AVAX-USD-PERP",
        "AAVE-USD-PERP", "LINK-USD-PERP", "UNI-USD-PERP", "ARB-USD-PERP",
        "DOGE-USD-PERP", "NEAR-USD-PERP", "SUI-USD-PERP", "LTC-USD-PERP",
        "XRP-USD-PERP", "ADA-USD-PERP", "ATOM-USD-PERP", "OP-USD-PERP",
        "INJ-USD-PERP", "FIL-USD-PERP", "APT-USD-PERP", "TIA-USD-PERP",
    ])
    cadence_seconds: float = 4 * 3600.0
    min_seconds_between_entries: float = 4 * 3600.0
    max_concurrent: int = 3
    max_trades_per_week: int = 10
    max_size_pct: float = 0.25         # at conviction=10
    offset_bps: float = 1.0


@dataclass
class AccountCState:
    last_briefing_ts: float = 0.0
    last_entry_ts: float = 0.0
    week_started_ts: float = 0.0
    trades_this_week: int = 0


class AccountCLLMScout(Strategy):
    name = "C_llm_scout"

    def __init__(
        self,
        llm_clients: List[LLMClient],
        config: AccountCConfig | None = None,
        decision_log=None,
    ):
        if len(llm_clients) < 2:
            raise ValueError("AccountC requires at least 2 LLM clients (consensus filter)")
        self.config = config or AccountCConfig()
        self.llms = llm_clients
        self.state = AccountCState()
        self.decision_log = decision_log
        self._current_cycle_id: str = ""

    def venues(self) -> List[str]:
        return [self.config.venue]

    def symbols(self, venue: str) -> List[str]:
        return list(self.config.universe) if venue == self.config.venue else []

    def evaluate(
        self, market: MarketState, portfolio: PortfolioSnapshot
    ) -> List[IntendedOrder]:
        # Reset weekly counter
        if market.ts - self.state.week_started_ts > 7 * 86400:
            self.state.week_started_ts = market.ts
            self.state.trades_this_week = 0

        if self.state.trades_this_week >= self.config.max_trades_per_week:
            return []
        if len(portfolio.positions) >= self.config.max_concurrent:
            return []
        # Cadence: only call LLMs every 4h
        if market.ts - self.state.last_briefing_ts < self.config.cadence_seconds:
            return []
        # Min spacing between entries
        if market.ts - self.state.last_entry_ts < self.config.min_seconds_between_entries:
            return []

        briefing = self._build_briefing(market, portfolio)
        self.state.last_briefing_ts = market.ts

        # Set cycle id BEFORE the LLM calls so logged_client picks it up
        import uuid
        self._current_cycle_id = f"{market.ts:.0f}_{uuid.uuid4().hex[:8]}"

        # Run both LLMs; only proceed on agreement
        try:
            results = [client(briefing) for client in self.llms]
        except Exception as e:
            if self.decision_log:
                self.decision_log.append_consensus(
                    briefing_ts=market.ts, cycle_id=self._current_cycle_id,
                    consensus_ideas=[], orders_placed=[],
                )
            return []

        consensus = _consensus(results)
        orders = self._ideas_to_orders(consensus, market, portfolio) if consensus else []

        if self.decision_log:
            self.decision_log.append_consensus(
                briefing_ts=market.ts, cycle_id=self._current_cycle_id,
                consensus_ideas=consensus, orders_placed=orders,
            )

        return orders

    def cycle_id(self) -> str:
        """Used by logged LLM clients to tag their call with the current cycle."""
        return self._current_cycle_id

    def _build_briefing(self, market: MarketState,
                        portfolio: PortfolioSnapshot) -> dict:
        funding_summary = []
        for sym in self.config.universe:
            f = market.funding.get((self.config.venue, sym))
            book = market.books.get((self.config.venue, sym))
            if f is None or book is None:
                continue
            funding_summary.append({
                "symbol": sym,
                "funding_bps_per_8h": f.rate_bps_per_8h,
                "mid": book.mid,
                "spread_bps": book.spread_bps,
            })

        # Compute BTC 4h regime from candles if available
        btc_closes = market.candles.get((self.config.venue, "BTC-USD-PERP", "1h_close"), [])
        btc_regime = "UNKNOWN"
        if len(btc_closes) >= 20:
            recent = sum(btc_closes[-4:]) / 4
            older = sum(btc_closes[-20:-16]) / 4
            if recent > older * 1.005:
                btc_regime = "TREND_UP"
            elif recent < older * 0.995:
                btc_regime = "TREND_DOWN"
            else:
                btc_regime = "CHOP"

        return {
            "ts": market.ts,
            "btc_regime": btc_regime,
            "open_positions": [
                {"symbol": p.symbol, "side": p.side, "size": p.size,
                 "entry_price": p.entry_price}
                for p in portfolio.positions
            ],
            "equity": portfolio.equity,
            "funding_per_symbol": funding_summary,
            "max_trades_remaining_this_week": (
                self.config.max_trades_per_week - self.state.trades_this_week),
        }

    def _ideas_to_orders(
        self, ideas: List[LLMTradeIdea], market: MarketState,
        portfolio: PortfolioSnapshot,
    ) -> List[IntendedOrder]:
        orders: List[IntendedOrder] = []
        symbols_held = {p.symbol for p in portfolio.positions}
        slots_available = self.config.max_concurrent - len(portfolio.positions)
        for idea in ideas[:slots_available]:
            if idea.symbol in symbols_held:
                continue
            book = market.books.get((self.config.venue, idea.symbol))
            if not book or book.mid is None:
                continue

            size_pct = self.config.max_size_pct * (idea.conviction / 10.0)
            position_usd = portfolio.equity * size_pct
            size_base = position_usd / book.mid

            offset = book.mid * self.config.offset_bps / 10_000.0
            if idea.direction == "LONG":
                price = (book.best_bid or book.mid) - offset
                side = "BUY"
            elif idea.direction == "SHORT":
                price = (book.best_ask or book.mid) + offset
                side = "SELL"
            else:
                continue

            orders.append(IntendedOrder(
                ts_decision=market.ts, venue=self.config.venue,
                symbol=idea.symbol, side=side, type="POST_ONLY",
                price=price, size=size_base,
                strategy_tag=f"C:conv={idea.conviction}:{idea.direction}",
            ))
            self.state.trades_this_week += 1
            self.state.last_entry_ts = market.ts
            if self.state.trades_this_week >= self.config.max_trades_per_week:
                break
        return orders


def _consensus(results: List[List[LLMTradeIdea]]) -> List[LLMTradeIdea]:
    """Return ideas that appear in every LLM result, matched by (symbol, direction).

    Uses the MIN conviction across LLMs (most conservative) and the FIRST LLM's thesis.
    """
    if not results or any(not r for r in results):
        return []
    by_key_first = {(i.symbol, i.direction): i for i in results[0]}
    consensus_keys = set(by_key_first.keys())
    for r in results[1:]:
        consensus_keys &= {(i.symbol, i.direction) for i in r}

    out: List[LLMTradeIdea] = []
    for key in consensus_keys:
        first = by_key_first[key]
        min_conv = first.conviction
        for r in results[1:]:
            for i in r:
                if (i.symbol, i.direction) == key:
                    min_conv = min(min_conv, i.conviction)
                    break
        out.append(LLMTradeIdea(
            symbol=key[0], direction=key[1], conviction=min_conv,
            thesis=first.thesis, time_horizon_hours=first.time_horizon_hours,
        ))
    out.sort(key=lambda i: -i.conviction)
    return out
