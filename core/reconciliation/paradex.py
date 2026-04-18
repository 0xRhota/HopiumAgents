"""Paradex reconciler.

Auth + API client pattern cribbed from scripts/pnl_tracker.py.
Paradex fields are strings (e.g. "74689.9") — always coerce to float.
Timestamps are millisecond-epoch ints.

Sign conventions from Paradex API:
- fee: positive = taker pays; negative = maker rebate. Matches our convention.
- realized_funding: positive = we received; negative = we paid.
  Our WindowPnL.funding_paid uses OPPOSITE sign (positive = paid),
  so we invert when mapping.
- realized_pnl: gross of fees, attributed to this fill.
  Opening fills have realized_pnl == 0 on Paradex; we translate that to None.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import List, Optional

from core.reconciliation.base import (
    ExchangeSnapshot,
    Fill,
    Position,
    Reconciler,
    WindowPnL,
)


def _ts_from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _f(x) -> float:
    """Coerce Paradex string numbers (or None) to float."""
    if x is None or x == "":
        return 0.0
    return float(x)


def _map_position(d: dict) -> Optional[Position]:
    if d.get("status") != "OPEN":
        return None
    size = _f(d.get("size"))
    if size == 0:
        return None
    side = d.get("side", "LONG").upper()
    return Position(
        exchange="paradex",
        symbol=d["market"],
        side=side,
        size=abs(size),
        entry_price=_f(d.get("average_entry_price")),
        unrealized_pnl=_f(d.get("unrealized_pnl")),
        # Paradex sign: unrealized_funding_pnl positive = received.
        # Our sign: funding_accrued positive = paid. Invert.
        funding_accrued=-_f(d.get("unrealized_funding_pnl")),
    )


def _map_fill(d: dict) -> Fill:
    """Translate a Paradex fill record to our Fill dataclass."""
    realized = _f(d.get("realized_pnl"))
    # Paradex opening fills have realized_pnl == "0". Treat 0 as None (opening).
    # Any nonzero → closing fill.
    opens_or_closes = "OPEN" if realized == 0 else "CLOSE"
    realized_out = None if realized == 0 else realized

    return Fill(
        exchange="paradex",
        symbol=d["market"],
        fill_id=d["id"],
        order_id=d.get("order_id", ""),
        ts=_ts_from_ms(d["created_at"]),
        side=d["side"],
        size=_f(d["size"]),
        price=_f(d["price"]),
        fee=_f(d["fee"]),  # Paradex sign matches ours
        is_maker=(d.get("liquidity") == "MAKER"),
        realized_pnl_usd=realized_out,
        opens_or_closes=opens_or_closes,
        linked_entry_fill_id=None,  # Paradex doesn't provide it; reconstruct later if needed
    )


class ParadexReconciler(Reconciler):
    """Pull authoritative state from Paradex.

    Construct with either:
      - a `client` (for testing; any object with .api_client.fetch_*())
      - nothing, in which case we build a ParadexSubkey from env vars
    """

    def __init__(self, client=None):
        self._client = client

    @property
    def exchange(self) -> str:
        return "paradex"

    def _lazy_client(self):
        if self._client is not None:
            return self._client
        from paradex_py import ParadexSubkey
        self._client = ParadexSubkey(
            env="prod",
            l2_private_key=os.getenv("PARADEX_PRIVATE_SUBKEY"),
            l2_address=os.getenv("PARADEX_ACCOUNT_ADDRESS"),
        )
        return self._client

    def _fetch_raw_fills(self) -> list:
        """Raw list from fetch_fills() — returned as list of dicts."""
        resp = self._lazy_client().api_client.fetch_fills()
        return (resp.get("results") if isinstance(resp, dict) else []) or []

    async def snapshot(self, since: Optional[datetime] = None) -> ExchangeSnapshot:
        api = self._lazy_client().api_client
        summary = api.fetch_account_summary()
        equity = _f(getattr(summary, "account_value", 0))

        positions_resp = api.fetch_positions()
        positions: List[Position] = [
            m for p in (positions_resp.get("results") if isinstance(positions_resp, dict) else []) or []
            for m in [_map_position(p)] if m is not None
        ]

        # Single pass over fills: map + funding-since accumulation together.
        # Paradex returns `realized_funding` positive = received; invert for our sign.
        new_fills: List[Fill] = []
        funding_since = 0.0
        for fd in self._fetch_raw_fills():
            fill = _map_fill(fd)
            if since is None or fill.ts >= since:
                new_fills.append(fill)
                if since is not None:
                    funding_since += -_f(fd.get("realized_funding"))

        return ExchangeSnapshot(
            exchange="paradex",
            ts=datetime.now(timezone.utc),
            equity=equity,
            positions=positions,
            new_fills=new_fills,
            funding_paid_since=funding_since,
        )

    async def get_pnl_window(self, hours: int) -> WindowPnL:
        from datetime import timedelta
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)

        realized = fees = funding_received = 0.0
        count = 0
        for fd in self._fetch_raw_fills():
            ts = _ts_from_ms(fd["created_at"])
            if start <= ts < end:
                realized += _f(fd.get("realized_pnl"))
                fees += _f(fd.get("fee"))
                funding_received += _f(fd.get("realized_funding"))
                count += 1

        return WindowPnL(
            exchange="paradex",
            window_start=start,
            window_end=end,
            realized_pnl=realized,
            fees_paid=fees,
            funding_paid=-funding_received,
            trade_count=count,
        )
