"""Paper sim runner — orchestrates the event loop.

Pipeline per cycle:
  1. Receive MarketEvent from a venue stream (book delta, trade, funding)
  2. Update internal state (BookMaintainer, FundingFeed, candle buffers)
  3. Drive AdverseSelectionTracker.poll
  4. Periodically (every `decision_interval_seconds`) call Strategy.evaluate
  5. For each IntendedOrder:
       - Sample latency → ts_arrived
       - Snapshot book at ts_arrived
       - FillEngine.place(order, book, ts_arrived)
       - If immediate fill → write to ledger + register adverse measurement
       - If resting → register, await consume_trade
  6. On every TradeTick: FillEngine.consume_trade → write any maker fills to ledger

This is async-driven by the venue stream. Multiple venues are merged via
asyncio.gather + an internal queue.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from paper_sim.core.adverse import AdverseSelectionTracker
from paper_sim.core.book import BookMaintainer
from paper_sim.core.fills import FillEngine
from paper_sim.core.latency import LatencyInjector, get_profile
from paper_sim.core.ledger import PaperLedger
from paper_sim.core.types import (
    BookSnapshot,
    FundingTick,
    IntendedOrder,
    PaperFill,
    Position,
    PortfolioSnapshot,
    TradeTick,
    VenueFees,
)
from paper_sim.strategies.base import MarketState, Strategy
from paper_sim.venues.base import (
    BookDelta,
    BookFullSnapshot,
    MarketEvent,
    VenueClient,
)
from paper_sim.venues.replay import RecorderVenue

logger = logging.getLogger(__name__)


# Default fee schedules (paper sim ground truth — keep in sync with venues)
DEFAULT_FEES: Dict[str, VenueFees] = {
    "paradex": VenueFees(venue="paradex", maker_bps=-0.5, taker_bps=2.0),
    "hyperliquid": VenueFees(venue="hyperliquid", maker_bps=2.0, taker_bps=5.0),
    "nado": VenueFees(venue="nado", maker_bps=1.0, taker_bps=3.5),
    "hibachi": VenueFees(venue="hibachi", maker_bps=0.0, taker_bps=35.0),
}


@dataclass
class RunnerConfig:
    account: str
    starting_equity: float = 5000.0
    decision_interval_seconds: float = 60.0    # how often to call Strategy.evaluate
    candle_intervals_seconds: Tuple[float, ...] = (60.0, 300.0, 900.0, 3600.0)
    candle_history_count: int = 200
    ledger_dir: str = "logs/paper"
    record_market_data: bool = True            # tee inputs to logs/paper/{venue}_l2_recording.jsonl
    seed: int = 42


@dataclass
class _Portfolio:
    cash: float
    positions: Dict[Tuple[str, str], Position] = field(default_factory=dict)
    cumulative_fees: float = 0.0
    cumulative_adverse: float = 0.0
    cumulative_funding: float = 0.0

    def equity(self, books: Dict[Tuple[str, str], BookMaintainer]) -> float:
        e = self.cash
        for key, p in self.positions.items():
            book = books.get(key)
            if book is None:
                continue
            snap = book.snapshot()
            mark = snap.mid
            if mark is None:
                continue
            signed = p.size if p.side == "BUY" else -p.size
            e += signed * (mark - p.entry_price)
        return e

    def snapshot(self, ts: float, account: str,
                 books: Dict[Tuple[str, str], BookMaintainer]) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            ts=ts, account=account,
            equity=self.equity(books), cash=self.cash,
            positions=tuple(self.positions.values()),
            cumulative_fees_paid=self.cumulative_fees,
            cumulative_adverse_cost=self.cumulative_adverse,
            cumulative_funding_paid=self.cumulative_funding,
        )

    def apply_fill(self, fill: PaperFill) -> None:
        key = (fill.venue, fill.symbol)
        existing = self.positions.get(key)

        if existing is None:
            self.positions[key] = Position(
                venue=fill.venue, symbol=fill.symbol, side=fill.side,
                size=fill.size, entry_price=fill.price, entry_ts=fill.ts_filled,
            )
        elif fill.side == existing.side:
            # Adding to position — weighted-average entry
            new_size = existing.size + fill.size
            total_notional = existing.entry_price * existing.size + \
                             fill.price * fill.size
            self.positions[key] = Position(
                venue=fill.venue, symbol=fill.symbol, side=existing.side,
                size=new_size, entry_price=total_notional / new_size,
                entry_ts=existing.entry_ts,
            )
        else:
            existing_sign = 1 if existing.side == "BUY" else -1
            if fill.size < existing.size:
                # Partial close
                pnl = fill.size * (fill.price - existing.entry_price) * existing_sign
                self.cash += pnl
                self.positions[key] = Position(
                    venue=fill.venue, symbol=fill.symbol, side=existing.side,
                    size=existing.size - fill.size,
                    entry_price=existing.entry_price, entry_ts=existing.entry_ts,
                )
            elif abs(fill.size - existing.size) < 1e-12:
                # Exact close — realized
                pnl = existing.size * (fill.price - existing.entry_price) * existing_sign
                self.cash += pnl
                del self.positions[key]
            else:
                # Flip — realize all of old, open new
                pnl = existing.size * (fill.price - existing.entry_price) * existing_sign
                self.cash += pnl
                self.positions[key] = Position(
                    venue=fill.venue, symbol=fill.symbol, side=fill.side,
                    size=fill.size - existing.size,
                    entry_price=fill.price, entry_ts=fill.ts_filled,
                )

        # Apply fee (negative = rebate received)
        self.cash -= fill.fee_paid_usd
        self.cumulative_fees += fill.fee_paid_usd


class PaperRunner:
    """Top-level runner. Subscribes to venues, drives strategy, writes ledger."""

    def __init__(
        self,
        strategy: Strategy,
        venue_clients: Dict[str, VenueClient],
        config: RunnerConfig,
        fees: Optional[Dict[str, VenueFees]] = None,
    ):
        self.strategy = strategy
        self.venues = venue_clients
        self.config = config
        self.fees = fees or DEFAULT_FEES

        self._books: Dict[Tuple[str, str], BookMaintainer] = {}
        self._fundings: Dict[Tuple[str, str], FundingTick] = {}
        self._candles: Dict[Tuple[str, str, str], Deque[float]] = defaultdict(
            lambda: deque(maxlen=config.candle_history_count)
        )
        self._candle_aggs: Dict[Tuple[str, str, float], Dict] = {}
        self._latency: Dict[str, LatencyInjector] = {}
        for venue in venue_clients.keys():
            try:
                self._latency[venue] = LatencyInjector(get_profile(venue),
                                                        seed=config.seed)
            except ValueError:
                pass

        self._funding_lookup = self._make_funding_lookup()
        self._engine = FillEngine(self.fees, self._funding_lookup)
        self._adverse = AdverseSelectionTracker(window_seconds=30.0)
        self._portfolio = _Portfolio(cash=config.starting_equity)
        ledger_path = f"{config.ledger_dir}/{config.account}_ledger.jsonl"
        self._ledger = PaperLedger(ledger_path, account=config.account)

        self._recorders: Dict[str, RecorderVenue] = {}
        if config.record_market_data:
            for v in venue_clients.keys():
                rec_path = f"{config.ledger_dir}/{v}_l2_recording.jsonl"
                self._recorders[v] = RecorderVenue(rec_path)

        self._last_decision_ts: float = 0.0
        self._closed = False

    def _make_funding_lookup(self):
        def lookup(venue: str, symbol: str, ts: float) -> float:
            f = self._fundings.get((venue, symbol))
            return f.rate_bps_per_8h if f else 0.0
        return lookup

    def _get_book(self, venue: str, symbol: str) -> BookMaintainer:
        key = (venue, symbol)
        if key not in self._books:
            self._books[key] = BookMaintainer(venue, symbol)
        return self._books[key]

    def _get_mid(self, venue: str, symbol: str) -> Optional[float]:
        book = self._books.get((venue, symbol))
        if book is None:
            return None
        return book.snapshot().mid

    async def run(self) -> None:
        for client in self.venues.values():
            await client.connect()

        try:
            tasks = []
            for venue, client in self.venues.items():
                symbols = self.strategy.symbols(venue)
                if not symbols:
                    continue
                tasks.append(asyncio.create_task(self._consume_venue(client, symbols)))
            await asyncio.gather(*tasks)
        finally:
            for client in self.venues.values():
                await client.close()
            for r in self._recorders.values():
                r.close()

    async def _consume_venue(self, client: VenueClient, symbols: List[str]) -> None:
        async for event in client.stream(symbols):
            if self._closed:
                break
            self._handle_event(event)

    def _handle_event(self, event: MarketEvent) -> None:
        if event.venue in self._recorders:
            self._recorders[event.venue].record(event)

        if isinstance(event, BookFullSnapshot):
            book = self._get_book(event.venue, event.symbol)
            book.apply_snapshot(event.ts,
                                [(p, s) for p, s in event.bids],
                                [(p, s) for p, s in event.asks])
            self._update_candles(event.venue, event.symbol, event.ts,
                                 mid=book.snapshot().mid, vol=0.0)

        elif isinstance(event, BookDelta):
            book = self._get_book(event.venue, event.symbol)
            book.apply_delta(event.ts, event.side, event.price, event.size)

        elif isinstance(event, TradeTick):
            self._update_candles(event.venue, event.symbol, event.ts,
                                 mid=event.price, vol=event.size)
            new_fills = self._engine.consume_trade(event)
            for f in new_fills:
                self._record_fill(f)

        elif isinstance(event, FundingTick):
            self._fundings[(event.venue, event.symbol)] = event

        # Poll adverse selection
        for fill_id, drift in self._adverse.poll(event.ts, self._get_mid):
            self._ledger.update_adverse(fill_id, drift)
            self._portfolio.cumulative_adverse += abs(min(0.0, drift))

        # Run strategy at decision cadence
        if event.ts - self._last_decision_ts >= self.config.decision_interval_seconds:
            self._last_decision_ts = event.ts
            self._run_strategy(event.ts)

    def _update_candles(self, venue: str, symbol: str, ts: float,
                        mid: Optional[float], vol: float) -> None:
        if mid is None:
            return
        for interval in self.config.candle_intervals_seconds:
            agg_key = (venue, symbol, interval)
            agg = self._candle_aggs.get(agg_key)
            bucket = int(ts // interval) * interval
            if agg is None or agg["bucket"] != bucket:
                if agg is not None:
                    interval_label = self._interval_label(interval)
                    self._candles[(venue, symbol, f"{interval_label}_close")].append(agg["close"])
                    self._candles[(venue, symbol, f"{interval_label}_high")].append(agg["high"])
                    self._candles[(venue, symbol, f"{interval_label}_low")].append(agg["low"])
                    self._candles[(venue, symbol, f"{interval_label}_vol")].append(agg["vol"])
                self._candle_aggs[agg_key] = {
                    "bucket": bucket, "open": mid, "high": mid, "low": mid,
                    "close": mid, "vol": vol,
                }
            else:
                agg["high"] = max(agg["high"], mid)
                agg["low"] = min(agg["low"], mid)
                agg["close"] = mid
                agg["vol"] += vol

    @staticmethod
    def _interval_label(seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds // 60)}m"
        return f"{int(seconds // 3600)}h"

    def _run_strategy(self, ts: float) -> None:
        market = MarketState(ts=ts)
        for key, book in self._books.items():
            market.books[key] = book.snapshot()
        market.funding = dict(self._fundings)
        for key, dq in self._candles.items():
            market.candles[key] = list(dq)

        portfolio = self._portfolio.snapshot(ts, self.config.account, self._books)
        try:
            orders = self.strategy.evaluate(market, portfolio)
        except Exception as e:
            logger.exception(f"strategy.evaluate raised: {e}")
            return

        for order in orders:
            self._place_order(order)

    def _place_order(self, order: IntendedOrder) -> None:
        latency = self._latency.get(order.venue)
        ts_arrived = order.ts_decision + (
            latency.sample_seconds() if latency else 0.2)

        book = self._get_book(order.venue, order.symbol).snapshot()
        try:
            result = self._engine.place(order, book, ts_arrived)
        except Exception as e:
            logger.warning(f"FillEngine.place raised: {e}")
            return

        if result.fill:
            self._record_fill(result.fill)
        elif result.rejected_reason:
            logger.debug(f"order rejected: {result.rejected_reason}")
        # resting orders are tracked by FillEngine; no immediate action

    def _record_fill(self, fill: PaperFill) -> None:
        self._ledger.append(fill)
        self._portfolio.apply_fill(fill)
        if fill.is_maker:
            self._adverse.register(
                fill_id=fill.fill_id, fill_ts=fill.ts_filled,
                fill_price=fill.price, side=fill.side,
                venue=fill.venue, symbol=fill.symbol,
            )

    def stop(self) -> None:
        self._closed = True

    def portfolio_snapshot(self) -> PortfolioSnapshot:
        return self._portfolio.snapshot(time.time(), self.config.account, self._books)
