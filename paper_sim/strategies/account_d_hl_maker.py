"""Account D — Hyperliquid BTC maker-rebate quoting.

Hypothesis: pure liquidity provision on a deep book pays floor returns
even at zero alpha. This account exists as the "always-positive" baseline
that runs alongside the others.

Mechanic:
  - Quote both sides at best-bid / best-ask continuously, size 0.005 BTC each
  - On book moves > 0.5 bp, cancel/replace
  - Inventory bands: skew quotes if position deviates > $1k either direction
  - Hard kill switch: if |inventory| > $2k, flatten via taker; pause 1h
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from paper_sim.core.types import IntendedOrder, PortfolioSnapshot
from paper_sim.strategies.base import MarketState, Strategy


@dataclass
class AccountDConfig:
    venue: str = "hyperliquid"
    symbol: str = "BTC"
    quote_size: float = 0.005           # ~$400 notional per side
    cancel_replace_threshold_bps: float = 0.5
    inventory_skew_threshold_usd: float = 1_000.0
    inventory_kill_threshold_usd: float = 2_000.0
    cooldown_seconds: float = 3_600.0
    skew_offset_bps: float = 1.0


@dataclass
class AccountDState:
    last_quote_bid: Optional[float] = None
    last_quote_ask: Optional[float] = None
    bid_order_id: Optional[str] = None
    ask_order_id: Optional[str] = None
    cooldown_until_ts: float = 0.0
    last_position_usd: float = 0.0


class AccountDHLMaker(Strategy):
    name = "D_hl_maker"

    def __init__(self, config: AccountDConfig | None = None):
        self.config = config or AccountDConfig()
        self.state = AccountDState()

    def venues(self) -> List[str]:
        return [self.config.venue]

    def symbols(self, venue: str) -> List[str]:
        return [self.config.symbol] if venue == self.config.venue else []

    def evaluate(
        self, market: MarketState, portfolio: PortfolioSnapshot
    ) -> List[IntendedOrder]:
        # Cooldown after kill switch
        if market.ts < self.state.cooldown_until_ts:
            return []

        book = market.books.get((self.config.venue, self.config.symbol))
        if book is None or book.best_bid is None or book.best_ask is None:
            return []

        # Compute inventory in USD
        inv_usd = 0.0
        for p in portfolio.positions:
            if p.venue == self.config.venue and p.symbol == self.config.symbol:
                signed_size = p.size if p.side == "BUY" else -p.size
                inv_usd += signed_size * p.entry_price
        self.state.last_position_usd = inv_usd

        # Kill switch
        if abs(inv_usd) > self.config.inventory_kill_threshold_usd:
            self.state.cooldown_until_ts = market.ts + self.config.cooldown_seconds
            self.state.bid_order_id = None
            self.state.ask_order_id = None
            return [self._flatten_order(book, inv_usd, market.ts)]

        # Build new quotes
        bid_px = book.best_bid
        ask_px = book.best_ask

        # Skew if inventory above threshold
        skew = 0.0
        if abs(inv_usd) > self.config.inventory_skew_threshold_usd:
            skew = book.mid * self.config.skew_offset_bps / 10_000.0
            if inv_usd > 0:
                # Long-skewed: lower bid (less buying), keep ask
                bid_px -= skew
            else:
                # Short-skewed: raise ask (less selling), keep bid
                ask_px += skew

        orders: List[IntendedOrder] = []

        # Bid side: place if no current quote or moved beyond threshold
        if self._needs_replace(self.state.last_quote_bid, bid_px, book.mid):
            orders.append(IntendedOrder(
                ts_decision=market.ts, venue=self.config.venue,
                symbol=self.config.symbol, side="BUY", type="POST_ONLY",
                price=bid_px, size=self.config.quote_size,
                strategy_tag="D:quote_bid",
                client_id=f"D_bid_{int(market.ts * 1000)}",
            ))
            self.state.last_quote_bid = bid_px

        # Ask side
        if self._needs_replace(self.state.last_quote_ask, ask_px, book.mid):
            orders.append(IntendedOrder(
                ts_decision=market.ts, venue=self.config.venue,
                symbol=self.config.symbol, side="SELL", type="POST_ONLY",
                price=ask_px, size=self.config.quote_size,
                strategy_tag="D:quote_ask",
                client_id=f"D_ask_{int(market.ts * 1000)}",
            ))
            self.state.last_quote_ask = ask_px

        return orders

    def _needs_replace(self, last: Optional[float], current: float,
                       mid: Optional[float]) -> bool:
        if mid is None or mid <= 0:
            return False
        if last is None:
            return True
        bps_moved = abs(last - current) / mid * 10_000.0
        return bps_moved >= self.config.cancel_replace_threshold_bps

    def _flatten_order(self, book, inv_usd: float, ts: float) -> IntendedOrder:
        # Flatten via taker (kill switch)
        size = abs(inv_usd) / book.mid if book.mid else self.config.quote_size
        side = "SELL" if inv_usd > 0 else "BUY"
        return IntendedOrder(
            ts_decision=ts, venue=self.config.venue,
            symbol=self.config.symbol, side=side, type="MARKET",
            size=size, strategy_tag="D:KILL_FLATTEN", reduce_only=True,
        )
