"""Nado reconciler.

Data source: Nado Archive API `matches` endpoint via NadoSDK._archive_query.
Existing get_pnl() aggregates the same endpoint — we extend to per-fill detail.

Scaling (Nado convention):
- Numbers are x18 scaled integers (strings). `_from_x18(v) = v / 1e18`.
- price = priceX18 / 1e18
- base_filled signed: negative = SELL
- quote_filled signed: negative = outflow (paid for a buy), positive = inflow (received from a sell)
- fee: positive = taker paid; maker fees on Nado are 1 bps paid (still positive)

Timestamps come from txs[] list, joined by submission_idx.

Match → Fill mapping:
- fill_id: digest
- order_id: digest (Nado doesn't separate — each match is one "order exec")
- side: SELL if base_filled < 0 else BUY
- size: abs(base_filled / 1e18)
- price: priceX18 / 1e18  (use quote_filled / base_filled as fallback)
- fee: _from_x18(fee)
- is_maker: not is_taker
- realized_pnl_usd: _from_x18(realized_pnl), None if 0 (opening)
- opens_or_closes: CLOSE if realized != 0 else OPEN
- symbol: PRODUCT_SYMBOLS[product_id] + "-PERP"
"""

from __future__ import annotations

import asyncio
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


class NadoReconciler(Reconciler):
    """Pull authoritative state from Nado via Archive API."""

    def __init__(self, sdk=None):
        self._sdk = sdk

    @property
    def exchange(self) -> str:
        return "nado"

    def _lazy_sdk(self):
        if self._sdk is not None:
            return self._sdk
        from dexes.nado.nado_sdk import NadoSDK
        self._sdk = NadoSDK(
            wallet_address=os.getenv("NADO_WALLET_ADDRESS"),
            linked_signer_private_key=os.getenv("NADO_LINKED_SIGNER_PRIVATE_KEY"),
            subaccount_name=os.getenv("NADO_SUBACCOUNT_NAME", "default"),
        )
        return self._sdk

    async def snapshot(self, since: Optional[datetime] = None) -> ExchangeSnapshot:
        sdk = self._lazy_sdk()

        matches_payload = {
            "matches": {
                "subaccounts": [sdk._get_subaccount_bytes32()],
                "limit": 500,
                "isolated": False,
            }
        }
        info, raw_pos, resp = await asyncio.gather(
            sdk.get_subaccount_info(),
            sdk.get_positions(),
            sdk._archive_query(matches_payload),
        )

        # equity: match NadoAdapter.get_equity() — assets - liabilities from healths[2], both x18 scaled
        equity = 0.0
        if info and "healths" in info:
            healths = info.get("healths", [])
            if len(healths) >= 3:
                assets_x18 = int(healths[2].get("assets", "0"))
                liabilities_x18 = int(healths[2].get("liabilities", "0"))
                equity = max(sdk._from_x18(assets_x18) - sdk._from_x18(liabilities_x18), 0.0)
        positions: List[Position] = []
        for p in raw_pos or []:
            amt = float(p.get("amount_float", 0))
            if amt == 0:
                continue
            side = "LONG" if amt > 0 else "SHORT"
            # v_quote_balance is x18 scaled; amount_float is already scaled. Reconstruct entry.
            vq_raw = p.get("v_quote_balance", 0)
            vq = sdk._from_x18(int(vq_raw)) if vq_raw else 0.0
            entry_price = (vq / -amt) if amt else 0.0
            sym = p.get("symbol", "UNKNOWN")
            if not sym.endswith("-PERP"):
                sym = f"{sym}-PERP"
            positions.append(Position(
                exchange="nado",
                symbol=sym,
                side=side,
                size=abs(amt),
                entry_price=abs(entry_price),
                unrealized_pnl=0.0,  # Nado doesn't expose per-position uPnL; equity change carries it
                funding_accrued=0.0,
            ))

        matches = resp.get("matches", []) if isinstance(resp, dict) else []
        txs = resp.get("txs", []) if isinstance(resp, dict) else []

        ts_by_idx = {}
        for tx in txs:
            idx = tx.get("submission_idx")
            t = tx.get("timestamp")
            if idx and t:
                ts_by_idx[str(idx)] = datetime.fromtimestamp(int(t), tz=timezone.utc)

        fills: List[Fill] = []
        for m in matches:
            product_id = (
                m.get("pre_balance", {}).get("base", {}).get("perp", {}).get("product_id")
                if isinstance(m.get("pre_balance"), dict) else None
            )
            symbol_base = sdk.PRODUCT_SYMBOLS.get(product_id, f"UNKNOWN-{product_id}")
            symbol = symbol_base if symbol_base.endswith("-PERP") else f"{symbol_base}-PERP"

            base_filled_raw = m.get("base_filled", "0")
            base_filled = sdk._from_x18(int(base_filled_raw)) if base_filled_raw else 0
            if base_filled == 0:
                continue

            side = "SELL" if base_filled < 0 else "BUY"
            size = abs(base_filled)
            price_x18 = m.get("order", {}).get("priceX18", "0")
            price = sdk._from_x18(int(price_x18)) if price_x18 else 0.0
            # Fallback: if price is clearly wrong, use quote / base
            if price <= 0 or price > 1e9:
                quote_filled = sdk._from_x18(int(m.get("quote_filled", "0")))
                if size > 0:
                    price = abs(quote_filled / base_filled)

            fee = sdk._from_x18(int(m.get("fee", "0") or "0"))
            realized = sdk._from_x18(int(m.get("realized_pnl", "0") or "0"))
            opens_or_closes = "CLOSE" if realized != 0 else "OPEN"
            realized_out = None if realized == 0 else realized

            sub_idx = str(m.get("submission_idx"))
            ts = ts_by_idx.get(sub_idx, datetime.now(timezone.utc))

            fills.append(Fill(
                exchange="nado",
                symbol=symbol,
                fill_id=m["digest"],
                order_id=m["digest"],
                ts=ts,
                side=side,
                size=size,
                price=price,
                fee=fee,
                is_maker=not bool(m.get("is_taker", True)),
                realized_pnl_usd=realized_out,
                opens_or_closes=opens_or_closes,
                linked_entry_fill_id=None,
            ))

        if since is not None:
            new_fills = [f for f in fills if f.ts >= since]
        else:
            new_fills = fills

        return ExchangeSnapshot(
            exchange="nado",
            ts=datetime.now(timezone.utc),
            equity=equity,
            positions=positions,
            new_fills=new_fills,
            funding_paid_since=0.0,  # Nado funding not separated in Archive matches endpoint
        )

    async def get_pnl_window(self, hours: int) -> WindowPnL:
        sdk = self._lazy_sdk()
        pnl = await sdk.get_pnl(hours=hours)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        return WindowPnL(
            exchange="nado",
            window_start=start,
            window_end=end,
            realized_pnl=float(pnl.get("realized_pnl", 0)),
            fees_paid=float(pnl.get("fees", 0)),
            funding_paid=0.0,
            trade_count=int(pnl.get("trade_count", 0)),
        )
