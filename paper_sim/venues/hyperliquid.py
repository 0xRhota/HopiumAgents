"""HyperliquidVenue — live L2 + trades + funding via Hyperliquid WebSocket.

Uses the `hyperliquid-python-sdk` package (official) for WS connectivity.
Subscriptions:
  - l2Book: {coin}     → orderbook snapshots (full state, periodic)
  - trades: {coin}     → trade tape
  - allMids → mid prices (low cost; useful as price-of-record)

Funding rates pulled via REST `/info` endpoint, polled every 60s.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, List, Optional

from paper_sim.core.types import FundingTick, TradeTick
from paper_sim.venues.base import (
    BookFullSnapshot,
    MarketEvent,
    VenueClient,
)

logger = logging.getLogger(__name__)

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"
HL_REST_URL = "https://api.hyperliquid.xyz/info"


class HyperliquidVenue(VenueClient):
    """Live Hyperliquid venue."""

    def __init__(self, ws_url: str = HL_WS_URL, rest_url: str = HL_REST_URL):
        self.ws_url = ws_url
        self.rest_url = rest_url
        self._ws = None
        self._session = None
        self._closed = False
        self._funding_poll_task: Optional[asyncio.Task] = None
        # Queue must be created inside the running loop. Deferred to connect().
        self._event_queue: Optional[asyncio.Queue] = None

    @property
    def venue(self) -> str:
        return "hyperliquid"

    async def connect(self) -> None:
        import aiohttp
        try:
            import websockets  # noqa: F401
        except ImportError as e:
            raise RuntimeError("websockets package required for HyperliquidVenue") from e
        self._event_queue = asyncio.Queue(maxsize=10_000)
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
        import json
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
                            try:
                                msg = json.loads(raw)
                            except Exception:
                                continue
                            await self._dispatch(msg)
                except Exception as e:
                    logger.warning(f"[hl_ws] connection error: {e}; reconnect in {backoff}s")
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
        import json
        for sym in symbols:
            for sub in (
                {"type": "l2Book", "coin": sym},
                {"type": "trades", "coin": sym},
            ):
                msg = {"method": "subscribe", "subscription": sub}
                await ws.send(json.dumps(msg))

    async def _dispatch(self, msg: dict) -> None:
        channel = msg.get("channel")
        data = msg.get("data") or {}

        if channel == "l2Book":
            coin = data.get("coin")
            if not coin:
                return
            ts_ms = float(data.get("time", time.time() * 1000))
            levels = data.get("levels", [[], []])
            bids = tuple((float(lvl["px"]), float(lvl["sz"]))
                         for lvl in levels[0])
            asks = tuple((float(lvl["px"]), float(lvl["sz"]))
                         for lvl in levels[1])
            await self._event_queue.put(BookFullSnapshot(
                ts=ts_ms / 1000.0, venue=self.venue, symbol=coin,
                bids=bids, asks=asks))
        elif channel == "trades":
            trades = data if isinstance(data, list) else []
            for t in trades:
                if not isinstance(t, dict):
                    continue
                coin = t.get("coin")
                if not coin:
                    continue
                aggressor = "BUY" if t.get("side") == "B" else "SELL"
                await self._event_queue.put(TradeTick(
                    ts=float(t.get("time", time.time() * 1000)) / 1000.0,
                    venue=self.venue, symbol=coin,
                    price=float(t["px"]), size=float(t["sz"]),
                    aggressor_side=aggressor,
                ))

    async def _poll_funding_loop(self, symbols: List[str]) -> None:
        while not self._closed:
            try:
                await self._poll_funding(symbols)
            except Exception as e:
                logger.warning(f"[hl_funding] poll error: {e}")
            await asyncio.sleep(60.0)

    async def _poll_funding(self, symbols: List[str]) -> None:
        if self._session is None:
            return
        try:
            async with self._session.post(
                self.rest_url, json={"type": "metaAndAssetCtxs"}, timeout=10
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
        except asyncio.TimeoutError:
            return

        if not isinstance(data, list) or len(data) < 2:
            return
        meta, asset_ctxs = data[0], data[1]
        universe = meta.get("universe", []) if isinstance(meta, dict) else []

        wanted = set(symbols)
        for asset, ctx in zip(universe, asset_ctxs):
            name = asset.get("name") if isinstance(asset, dict) else None
            if name not in wanted or not isinstance(ctx, dict):
                continue
            # Hyperliquid funding is rate per hour. Convert to bps per 8h.
            rate_per_hour = float(ctx.get("funding", 0.0))
            rate_per_8h_bps = rate_per_hour * 8 * 10_000.0
            await self._event_queue.put(FundingTick(
                ts=time.time(), venue=self.venue, symbol=name,
                rate_bps_per_8h=rate_per_8h_bps,
            ))
