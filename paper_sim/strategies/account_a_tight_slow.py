"""Account A — Tight slow preset, multi-asset, no LLM.

Hypothesis: tightening the slow preset (4-of-5 signal alignment, longer holds,
ATR-adaptive stops) on a curated 20-symbol universe captures the narrow edge
we observed without overfitting to a single market.

Hard caps:
  - max 5 trades/symbol/week (frequency kill)
  - max 3 concurrent positions
  - 25% equity per position
  - score_min 4.5 (4-of-5)
  - 48h max hold
  - ATR exits: tp=5.0× ATR, sl=2.5× ATR, floors 200/100 bps
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from paper_sim.core.types import IntendedOrder, PortfolioSnapshot
from paper_sim.strategies.base import MarketState, Strategy
from paper_sim.strategies.signals import atr_bps, direction_from_state, momentum_score


PARADEX_TOP20 = [
    "BTC-USD-PERP", "ETH-USD-PERP", "SOL-USD-PERP", "AVAX-USD-PERP",
    "AAVE-USD-PERP", "LINK-USD-PERP", "UNI-USD-PERP", "ARB-USD-PERP",
    "DOGE-USD-PERP", "NEAR-USD-PERP", "SUI-USD-PERP", "LTC-USD-PERP",
    "XRP-USD-PERP", "ADA-USD-PERP", "ATOM-USD-PERP", "OP-USD-PERP",
    "INJ-USD-PERP", "FIL-USD-PERP", "APT-USD-PERP", "TIA-USD-PERP",
]


@dataclass
class AccountAConfig:
    score_min: float = 4.5
    max_positions: int = 3
    max_trades_per_symbol_per_week: int = 5
    size_pct: float = 0.25
    max_hold_minutes: float = 48 * 60.0
    tp_atr_mult: float = 5.0
    sl_atr_mult: float = 2.5
    tp_bps_floor: float = 200.0
    sl_bps_floor: float = 100.0
    offset_bps: float = 1.0       # how far inside spread to post
    venue: str = "paradex"


@dataclass
class AccountAState:
    trade_count_by_symbol: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    week_started_ts: float = 0.0


class AccountATightSlow(Strategy):
    name = "A_tight_slow"

    def __init__(self, config: AccountAConfig | None = None,
                 universe: List[str] | None = None):
        self.config = config or AccountAConfig()
        self.universe = universe or PARADEX_TOP20
        self.state = AccountAState()

    def venues(self) -> List[str]:
        return [self.config.venue]

    def symbols(self, venue: str) -> List[str]:
        if venue != self.config.venue:
            return []
        return list(self.universe)

    def evaluate(
        self, market: MarketState, portfolio: PortfolioSnapshot
    ) -> List[IntendedOrder]:
        # Reset weekly counters
        if market.ts - self.state.week_started_ts > 7 * 86400:
            self.state.trade_count_by_symbol.clear()
            self.state.week_started_ts = market.ts

        # Cap by current open positions
        if len(portfolio.positions) >= self.config.max_positions:
            return []

        symbols_with_position = {p.symbol for p in portfolio.positions}
        intended: List[IntendedOrder] = []

        for sym in self.universe:
            if sym in symbols_with_position:
                continue
            if self.state.trade_count_by_symbol[sym] >= \
                    self.config.max_trades_per_symbol_per_week:
                continue

            book = market.books.get((self.config.venue, sym))
            if book is None or book.mid is None:
                continue

            closes = market.candles.get((self.config.venue, sym, "5m_close"), [])
            highs = market.candles.get((self.config.venue, sym, "5m_high"), [])
            lows = market.candles.get((self.config.venue, sym, "5m_low"), [])
            vols = market.candles.get((self.config.venue, sym, "5m_vol"), [])
            if len(closes) < 30:
                continue

            score = momentum_score(closes, highs, lows, vols)
            if score is None or score < self.config.score_min:
                continue
            direction = direction_from_state(closes)
            if direction == "FLAT":
                continue

            # Position size
            position_usd = portfolio.equity * self.config.size_pct
            mid = book.mid
            assert mid is not None
            size_base = position_usd / mid

            # Place POST_ONLY one offset inside the spread
            offset = mid * self.config.offset_bps / 10_000.0
            if direction == "LONG":
                price = (book.best_bid or mid) - offset
                side = "BUY"
            else:
                price = (book.best_ask or mid) + offset
                side = "SELL"

            intended.append(IntendedOrder(
                ts_decision=market.ts,
                venue=self.config.venue,
                symbol=sym,
                side=side, type="POST_ONLY",
                price=price, size=size_base,
                strategy_tag=f"A:score={score:.2f}:{direction}",
            ))
            self.state.trade_count_by_symbol[sym] += 1
            if len(intended) + len(portfolio.positions) >= self.config.max_positions:
                break

        return intended
