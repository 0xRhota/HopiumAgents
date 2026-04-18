"""Hibachi reconciler.

Data source: GET /trade/account/trades (verified 2026-04-17). Our SDK
wrapper didn't expose it — we call `sdk._request("GET", "/trade/account/trades", ...)`
directly.

Hibachi fill schema (quirks):
- side: "Buy" / "Sell" (mixed case, different from other exchanges)
- fee: string, already in USD (no x18 scaling)
- realizedPnl: string, "0.000000" for opening fills
- timestamp: seconds epoch
- bidAccountId / askAccountId: we're whichever matches our accountId
- bidOrderId / askOrderId: pick the one matching our side
- orderType: "MARKET" or "LIMIT"

Hibachi positions:
- get_positions() returns {symbol, side, amount, openPrice, unrealizedTradingPnl}
  where openPrice == entry_price (verified 2026-04-17).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from core.reconciliation.base import (
    ExchangeSnapshot,
    Fill,
    Position,
    Reconciler,
    WindowPnL,
)


def _map_hibachi_fill(t: dict, account_id: int) -> Fill:
    side = "BUY" if t.get("side", "").upper() == "BUY" else "SELL"
    is_ours_bid = t.get("bidAccountId") == account_id
    order_id = str(t.get("bidOrderId" if is_ours_bid else "askOrderId", ""))
    realized = float(t.get("realizedPnl", 0) or 0)
    return Fill(
        exchange="hibachi",
        symbol=t.get("symbol", "UNKNOWN"),
        fill_id=str(t.get("id")),
        order_id=order_id,
        ts=datetime.fromtimestamp(int(t.get("timestamp", 0)), tz=timezone.utc),
        side=side,
        size=float(t.get("quantity", 0)),
        price=float(t.get("price", 0)),
        fee=float(t.get("fee", 0) or 0),
        is_maker=not bool(t.get("is_taker", True)),
        realized_pnl_usd=None if realized == 0 else realized,
        opens_or_closes="CLOSE" if realized != 0 else "OPEN",
        linked_entry_fill_id=None,
    )


class HibachiReconciler(Reconciler):
    """Pull authoritative state from Hibachi."""

    def __init__(self, sdk=None):
        self._sdk = sdk

    @property
    def exchange(self) -> str:
        return "hibachi"

    def _lazy_sdk(self):
        if self._sdk is not None:
            return self._sdk
        from dexes.hibachi.hibachi_sdk import HibachiSDK
        self._sdk = HibachiSDK(
            api_key=os.getenv("HIBACHI_PUBLIC_KEY"),
            api_secret=os.getenv("HIBACHI_PRIVATE_KEY"),
            account_id=os.getenv("HIBACHI_ACCOUNT_ID"),
        )
        return self._sdk

    async def snapshot(self, since: Optional[datetime] = None) -> ExchangeSnapshot:
        sdk = self._lazy_sdk()
        account_id = sdk.get_account_id()

        equity_raw = await sdk.get_balance()
        equity = float(equity_raw) if equity_raw is not None else 0.0

        raw_pos = await sdk.get_positions()
        positions: List[Position] = []
        for p in raw_pos or []:
            size = float(p.get("amount") or p.get("size", 0))
            if size == 0:
                continue
            side = p.get("side", "LONG")
            if side not in ("LONG", "SHORT"):
                side = "LONG" if size > 0 else "SHORT"
            positions.append(Position(
                exchange="hibachi",
                symbol=p.get("symbol", "UNKNOWN"),
                side=side,
                size=abs(size),
                entry_price=float(p.get("openPrice") or p.get("entryPrice", 0)),
                unrealized_pnl=float(p.get("unrealizedTradingPnl") or p.get("unrealizedPnl", 0)),
                funding_accrued=0.0,  # Hibachi doesn't split out funding per-position
            ))

        fills = await self._fetch_fills(account_id, since=since)

        return ExchangeSnapshot(
            exchange="hibachi",
            ts=datetime.now(timezone.utc),
            equity=equity,
            positions=positions,
            new_fills=fills,
            funding_paid_since=0.0,
        )

    async def _fetch_fills(self, account_id: int, since: Optional[datetime] = None) -> List[Fill]:
        sdk = self._lazy_sdk()
        resp = await sdk._request(
            "GET", "/trade/account/trades",
            params={"accountId": account_id},
        )
        raw_trades = resp.get("trades", []) if isinstance(resp, dict) else []
        fills = [_map_hibachi_fill(t, account_id) for t in raw_trades]
        return [f for f in fills if since is None or f.ts >= since]

    async def get_pnl_window(self, hours: int) -> WindowPnL:
        sdk = self._lazy_sdk()
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        fills = await self._fetch_fills(sdk.get_account_id(), since=start)
        realized = sum(f.realized_pnl_usd or 0 for f in fills)
        fees = sum(f.fee for f in fills)
        return WindowPnL(
            exchange="hibachi",
            window_start=start,
            window_end=end,
            realized_pnl=realized,
            fees_paid=fees,
            funding_paid=0.0,
            trade_count=len(fills),
        )
