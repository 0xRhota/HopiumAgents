"""Account B — Funding arbitrage on BTC across Hyperliquid + Paradex.

Hypothesis: cross-venue funding spreads on BTC are a real, market-neutral
yield source. When funding diverges meaningfully between venues, open
delta-neutral pair (long on negative-funding venue, short on positive-funding
venue), collect spread, close when spread compresses.

Constraints:
  - Single symbol (BTC) on two venues
  - POST_ONLY entries on both legs
  - Size sets total notional ≈ $2,500 per leg
  - Open trigger: |Δfunding| > 3 bps annualized over 8h window
  - Close trigger: spread inverts > 1 cycle, or either leg moves > 1% intraday
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from paper_sim.core.types import IntendedOrder, PortfolioSnapshot
from paper_sim.strategies.base import MarketState, Strategy


@dataclass
class AccountBConfig:
    symbol: str = "BTC"
    paradex_symbol: str = "BTC-USD-PERP"
    hl_symbol: str = "BTC"
    open_threshold_bps: float = 3.0     # spread to trigger entry
    close_threshold_bps: float = 0.5    # spread compression to trigger exit
    leg_notional_usd: float = 2_500.0
    offset_bps: float = 1.0


@dataclass
class AccountBState:
    pair_open: bool = False
    long_venue: Optional[str] = None
    short_venue: Optional[str] = None
    last_open_ts: float = 0.0


class AccountBFundingArb(Strategy):
    name = "B_funding_arb"

    def __init__(self, config: AccountBConfig | None = None):
        self.config = config or AccountBConfig()
        self.state = AccountBState()

    def venues(self) -> List[str]:
        return ["paradex", "hyperliquid"]

    def symbols(self, venue: str) -> List[str]:
        if venue == "paradex":
            return [self.config.paradex_symbol]
        if venue == "hyperliquid":
            return [self.config.hl_symbol]
        return []

    def evaluate(
        self, market: MarketState, portfolio: PortfolioSnapshot
    ) -> List[IntendedOrder]:
        f_par = market.funding.get(("paradex", self.config.paradex_symbol))
        f_hl = market.funding.get(("hyperliquid", self.config.hl_symbol))
        if f_par is None or f_hl is None:
            return []
        spread_bps = abs(f_par.rate_bps_per_8h - f_hl.rate_bps_per_8h)

        if not self.state.pair_open:
            if spread_bps < self.config.open_threshold_bps:
                return []
            return self._open_pair(f_par, f_hl, market, portfolio)

        # Pair open: check exit conditions
        if spread_bps < self.config.close_threshold_bps:
            return self._close_pair(market, portfolio)
        return []

    def _open_pair(self, f_par, f_hl, market, portfolio):
        # Long the venue with NEGATIVE funding (we receive payments).
        # Short the venue with POSITIVE funding.
        if f_par.rate_bps_per_8h < f_hl.rate_bps_per_8h:
            long_venue, long_sym = "paradex", self.config.paradex_symbol
            short_venue, short_sym = "hyperliquid", self.config.hl_symbol
        else:
            long_venue, long_sym = "hyperliquid", self.config.hl_symbol
            short_venue, short_sym = "paradex", self.config.paradex_symbol

        long_book = market.books.get((long_venue, long_sym))
        short_book = market.books.get((short_venue, short_sym))
        if not long_book or not short_book:
            return []
        if long_book.mid is None or short_book.mid is None:
            return []

        long_size = self.config.leg_notional_usd / long_book.mid
        short_size = self.config.leg_notional_usd / short_book.mid

        long_offset = long_book.mid * self.config.offset_bps / 10_000.0
        short_offset = short_book.mid * self.config.offset_bps / 10_000.0

        long_price = (long_book.best_bid or long_book.mid) - long_offset
        short_price = (short_book.best_ask or short_book.mid) + short_offset

        self.state.pair_open = True
        self.state.long_venue = long_venue
        self.state.short_venue = short_venue
        self.state.last_open_ts = market.ts

        return [
            IntendedOrder(
                ts_decision=market.ts, venue=long_venue, symbol=long_sym,
                side="BUY", type="POST_ONLY", price=long_price, size=long_size,
                strategy_tag=f"B:long:{long_venue}",
            ),
            IntendedOrder(
                ts_decision=market.ts, venue=short_venue, symbol=short_sym,
                side="SELL", type="POST_ONLY", price=short_price, size=short_size,
                strategy_tag=f"B:short:{short_venue}",
            ),
        ]

    def _close_pair(self, market, portfolio):
        orders = []
        for pos in portfolio.positions:
            book = market.books.get((pos.venue, pos.symbol))
            if not book or book.mid is None:
                continue
            offset = book.mid * self.config.offset_bps / 10_000.0
            if pos.side == "BUY":
                # Close LONG via SELL POST_ONLY above mid
                price = (book.best_ask or book.mid) + offset
                side = "SELL"
            else:
                price = (book.best_bid or book.mid) - offset
                side = "BUY"
            orders.append(IntendedOrder(
                ts_decision=market.ts, venue=pos.venue, symbol=pos.symbol,
                side=side, type="POST_ONLY", price=price, size=pos.size,
                strategy_tag="B:close", reduce_only=True,
            ))
        # Reset state regardless — we'll re-open if conditions return
        self.state.pair_open = False
        self.state.long_venue = None
        self.state.short_venue = None
        return orders
