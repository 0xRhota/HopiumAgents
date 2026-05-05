"""Account E — ETH/BTC pair statistical arbitrage on Paradex.

Hypothesis: ETH/BTC price ratio is cointegrated. When the ratio diverges
significantly from its rolling mean (Z-score > 2 in either direction),
take a market-neutral pair position betting on mean reversion. Close at
|Z| < 0.5; stop out at |Z| > 4 (cointegration likely broken).

Evidence: published research on BTC-ETH pair trading shows Sharpe ~2.45,
~16% annualized returns at 0.15% transaction cost in backtest. Break-even
TC ~67 bps; our actual round-trip TC on Paradex with maker rebates is
~10 bps, leaving substantial margin.

Constraints:
  - Both legs POST_ONLY on Paradex (zero/rebate fees)
  - Equal-dollar notional per leg (delta-neutral on $)
  - Single open pair at a time
  - Rolling 24h window (288 × 5m bars) for ratio statistics
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import List, Optional

from paper_sim.core.types import IntendedOrder, PortfolioSnapshot
from paper_sim.strategies.base import MarketState, Strategy


@dataclass
class AccountEConfig:
    venue: str = "paradex"
    btc_symbol: str = "BTC-USD-PERP"
    eth_symbol: str = "ETH-USD-PERP"
    min_history_bars: int = 60               # ~5h of 5m candles before trading
    window_bars: int = 288                   # 24h rolling window on 5m candles
    z_open_threshold: float = 2.0
    z_close_threshold: float = 0.5
    z_stop_threshold: float = 4.0            # circuit-breaker if cointegration broken
    leg_notional_usd: float = 2_000.0        # per-leg $; total deployed = $4k of $5k
    offset_bps: float = 1.0
    min_seconds_between_attempts: float = 300.0


@dataclass
class AccountEState:
    pair_open: bool = False
    pair_direction: str = ""       # "ETH_LONG_BTC_SHORT" or "ETH_SHORT_BTC_LONG"
    last_attempt_ts: float = 0.0
    open_z_score: float = 0.0


class AccountEEthBtcPair(Strategy):
    name = "E_eth_btc_pair"

    def __init__(self, config: AccountEConfig | None = None):
        self.config = config or AccountEConfig()
        self.state = AccountEState()

    def venues(self) -> List[str]:
        return [self.config.venue]

    def symbols(self, venue: str) -> List[str]:
        if venue != self.config.venue:
            return []
        return [self.config.btc_symbol, self.config.eth_symbol]

    def evaluate(
        self, market: MarketState, portfolio: PortfolioSnapshot
    ) -> List[IntendedOrder]:
        # Throttle decisions
        if market.ts - self.state.last_attempt_ts < self.config.min_seconds_between_attempts:
            return []

        btc_closes = market.candles.get(
            (self.config.venue, self.config.btc_symbol, "5m_close"), [])
        eth_closes = market.candles.get(
            (self.config.venue, self.config.eth_symbol, "5m_close"), [])

        if len(btc_closes) < self.config.min_history_bars or \
                len(eth_closes) < self.config.min_history_bars:
            return []

        z = self._compute_z(btc_closes, eth_closes)
        if z is None:
            return []

        # Position management
        if self.state.pair_open:
            if abs(z) <= self.config.z_close_threshold:
                self.state.last_attempt_ts = market.ts
                return self._close_pair(market, portfolio)
            if abs(z) >= self.config.z_stop_threshold:
                # Cointegration likely broken — emergency close
                self.state.last_attempt_ts = market.ts
                return self._close_pair(market, portfolio)
            return []  # hold

        # Pair not open — check entry threshold
        if abs(z) < self.config.z_open_threshold:
            return []
        self.state.last_attempt_ts = market.ts
        self.state.open_z_score = z
        return self._open_pair(z, market, portfolio)

    def _compute_z(self, btc_closes: List[float],
                   eth_closes: List[float]) -> Optional[float]:
        n = min(len(btc_closes), len(eth_closes), self.config.window_bars)
        if n < self.config.min_history_bars:
            return None
        # Align tails
        btc_tail = btc_closes[-n:]
        eth_tail = eth_closes[-n:]
        ratios = [e / b for e, b in zip(eth_tail, btc_tail) if b > 0]
        if len(ratios) < 10:
            return None
        try:
            mean = statistics.mean(ratios)
            stdev = statistics.stdev(ratios)
        except statistics.StatisticsError:
            return None
        current = ratios[-1]
        if stdev <= 1e-12:
            # Ratios are flat — interpret as "perfectly at mean" (z = 0).
            # This lets an open pair exit cleanly when the spread has
            # collapsed; without it we'd be stuck holding.
            return 0.0
        return (current - mean) / stdev

    def _open_pair(
        self, z: float, market: MarketState, portfolio: PortfolioSnapshot,
    ) -> List[IntendedOrder]:
        btc_book = market.books.get((self.config.venue, self.config.btc_symbol))
        eth_book = market.books.get((self.config.venue, self.config.eth_symbol))
        if not btc_book or not eth_book:
            return []
        if btc_book.mid is None or eth_book.mid is None:
            return []

        btc_size = self.config.leg_notional_usd / btc_book.mid
        eth_size = self.config.leg_notional_usd / eth_book.mid

        btc_offset = btc_book.mid * self.config.offset_bps / 10_000.0
        eth_offset = eth_book.mid * self.config.offset_bps / 10_000.0

        # Z > 0 means ETH expensive vs BTC → short ETH, long BTC
        # Z < 0 means ETH cheap vs BTC → long ETH, short BTC
        if z > 0:
            # ETH SHORT, BTC LONG
            self.state.pair_direction = "ETH_SHORT_BTC_LONG"
            eth_price = (eth_book.best_ask or eth_book.mid) + eth_offset
            btc_price = (btc_book.best_bid or btc_book.mid) - btc_offset
            orders = [
                IntendedOrder(
                    ts_decision=market.ts, venue=self.config.venue,
                    symbol=self.config.eth_symbol, side="SELL", type="POST_ONLY",
                    price=eth_price, size=eth_size,
                    strategy_tag=f"E:open:eth_short:z={z:.2f}",
                ),
                IntendedOrder(
                    ts_decision=market.ts, venue=self.config.venue,
                    symbol=self.config.btc_symbol, side="BUY", type="POST_ONLY",
                    price=btc_price, size=btc_size,
                    strategy_tag=f"E:open:btc_long:z={z:.2f}",
                ),
            ]
        else:
            # ETH LONG, BTC SHORT
            self.state.pair_direction = "ETH_LONG_BTC_SHORT"
            eth_price = (eth_book.best_bid or eth_book.mid) - eth_offset
            btc_price = (btc_book.best_ask or btc_book.mid) + btc_offset
            orders = [
                IntendedOrder(
                    ts_decision=market.ts, venue=self.config.venue,
                    symbol=self.config.eth_symbol, side="BUY", type="POST_ONLY",
                    price=eth_price, size=eth_size,
                    strategy_tag=f"E:open:eth_long:z={z:.2f}",
                ),
                IntendedOrder(
                    ts_decision=market.ts, venue=self.config.venue,
                    symbol=self.config.btc_symbol, side="SELL", type="POST_ONLY",
                    price=btc_price, size=btc_size,
                    strategy_tag=f"E:open:btc_short:z={z:.2f}",
                ),
            ]
        self.state.pair_open = True
        return orders

    def _close_pair(
        self, market: MarketState, portfolio: PortfolioSnapshot,
    ) -> List[IntendedOrder]:
        orders = []
        for pos in portfolio.positions:
            book = market.books.get((pos.venue, pos.symbol))
            if not book or book.mid is None:
                continue
            offset = book.mid * self.config.offset_bps / 10_000.0
            if pos.side == "BUY":
                price = (book.best_ask or book.mid) + offset
                close_side = "SELL"
            else:
                price = (book.best_bid or book.mid) - offset
                close_side = "BUY"
            orders.append(IntendedOrder(
                ts_decision=market.ts, venue=pos.venue, symbol=pos.symbol,
                side=close_side, type="POST_ONLY",
                price=price, size=pos.size,
                strategy_tag="E:close", reduce_only=True,
            ))
        # Reset state
        self.state.pair_open = False
        self.state.pair_direction = ""
        return orders
