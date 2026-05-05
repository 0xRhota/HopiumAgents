"""ParadexVenue — live L2 + trades + funding via Paradex WebSocket.

Wraps Paradex JSON-RPC WebSocket subscriptions:
  - orderbook.{market}.SNAPSHOT_15  (15-level depth, our top 15 → matches book max_levels)
  - trades.{market}
  - markets_summary  (funding rate, periodic REST poll)

Connection lifecycle: connect() opens WS + warms initial book; stream() yields events.
On disconnect, reconnects with exponential backoff and resyncs book.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator, Dict, List, Optional

from paper_sim.core.types import FundingTick, TradeTick
from paper_sim.venues.base import (
    BookDelta,
    BookFullSnapshot,
    MarketEvent,
    VenueClient,
)

logger = logging.getLogger(__name__)

PARADEX_WS_URL = "wss://ws.api.prod.paradex.trade/v1"
PARADEX_REST_URL = "https://api.prod.paradex.trade/v1"


class ParadexVenue(VenueClient):
    """Live Paradex venue. Requires `aiohttp` and `websockets`."""

    def __init__(self, ws_url: str = PARADEX_WS_URL,
                 rest_url: str = PARADEX_REST_URL):
        self.ws_url = ws_url
        self.rest_url = rest_url
        self._ws = None
        self._session = None
        self._closed = False
        self._funding_poll_task: Optional[asyncio.Task] = None
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)

    @property
    def venue(self) -> str:
        return "paradex"

    async def connect(self) -> None:
        import aiohttp
        try:
            import websockets  # noqa: F401
        except ImportError as e:
            raise RuntimeError("websockets package required for ParadexVenue") from e
        self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        self._closed = True
        if self._funding_poll_task:
            self._funding_poll_task.cancel()
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()

    async def stream(self, symbols: List[str]) -> AsyncIterator[MarketEvent]:
        import websockets

        async def reader_loop():
            backoff = 1.0
            while not self._closed:
                try:
                    async with websockets.connect(self.ws_url) as ws:
                        self._ws = ws
                        await self._subscribe(ws, symbols)
                        backoff = 1.0
                        async for raw in ws:
                            await self._dispatch(raw)
                except Exception as e:
                    logger.warning(f"[paradex_ws] connection error: {e}; reconnect in {backoff}s")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

        reader = asyncio.create_task(reader_loop())
        self._funding_poll_task = asyncio.create_task(
            self._poll_funding_loop(symbols))

        try:
            while not self._closed:
                event = await self._event_queue.get()
                yield event
        finally:
            reader.cancel()
            if self._funding_poll_task:
                self._funding_poll_task.cancel()

    async def _subscribe(self, ws, symbols: List[str]) -> None:
        for sym in symbols:
            for channel in (f"order_book.{sym}.SNAPSHOT_15",
                            f"trades.{sym}"):
                msg = {"jsonrpc": "2.0", "id": int(time.time() * 1000),
                       "method": "subscribe", "params": {"channel": channel}}
                await ws.send(json.dumps(msg))

    async def _dispatch(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if "params" not in msg:
            return
        params = msg["params"]
        channel = params.get("channel", "")
        data = params.get("data") or {}
        ts = float(data.get("last_updated_at", time.time() * 1000)) / 1000.0
        sym_part = channel.split(".", 2)
        if len(sym_part) < 2:
            return
        symbol = sym_part[1]

        if channel.startswith("order_book."):
            bids = tuple((float(p), float(s))
                         for p, s in data.get("bids", []) or [])
            asks = tuple((float(p), float(s))
                         for p, s in data.get("asks", []) or [])
            await self._event_queue.put(BookFullSnapshot(
                ts=ts, venue=self.venue, symbol=symbol,
                bids=bids, asks=asks))
        elif channel.startswith("trades."):
            for trade in data.get("trades", []) or [data]:
                if not isinstance(trade, dict):
                    continue
                if "price" not in trade:
                    continue
                aggressor = trade.get("side", "BUY").upper()
                if aggressor not in ("BUY", "SELL"):
                    continue
                await self._event_queue.put(TradeTick(
                    ts=float(trade.get("created_at", ts * 1000)) / 1000.0,
                    venue=self.venue, symbol=symbol,
                    price=float(trade["price"]),
                    size=float(trade.get("size", trade.get("amount", 0))),
                    aggressor_side=aggressor,
                ))

    async def _poll_funding_loop(self, symbols: List[str]) -> None:
        logger.info(f"[paradex_funding] loop start; {len(symbols)} symbols")
        while not self._closed:
            try:
                count = await self._poll_funding(symbols)
                logger.info(f"[paradex_funding] polled OK; {count}/{len(symbols)} symbols got funding")
            except Exception as e:
                logger.warning(f"[paradex_funding] poll error: {e!r}")
            await asyncio.sleep(60.0)

    async def _poll_funding(self, symbols: List[str]) -> int:
        if self._session is None:
            logger.warning("[paradex_funding] _session is None; cannot poll")
            return 0
        ok = 0
        for sym in symbols:
            try:
                async with self._session.get(
                    f"{self.rest_url}/markets/summary",
                    params={"market": sym},
                    timeout=10,
                ) as resp:
                    if resp.status != 200:
                        if resp.status in (429, 503):
                            logger.warning(f"[paradex_funding] {sym} got {resp.status}")
                        continue
                    data = await resp.json()
                    results = data.get("results", []) if isinstance(data, dict) else []
                    if not results:
                        continue
                    rec = results[0]
                    rate = float(rec.get("funding_rate", 0.0))
                    # Paradex funding is per 8h; convert to bps
                    bps = rate * 10_000.0
                    await self._event_queue.put(FundingTick(
                        ts=time.time(), venue=self.venue, symbol=sym,
                        rate_bps_per_8h=bps,
                    ))
                    ok += 1
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.warning(f"[paradex_funding] {sym} fail: {e!r}")
                continue
        return ok
