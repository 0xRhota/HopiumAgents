"""Hibachi reconciler must paginate /trade/account/trades for windows longer than one page.

API quirks verified live 2026-04-18:
- Default page size: 100
- limit=500 is honored and returned
- endTime param filters to trades with ts < endTime (seconds epoch)
- No cursor/beforeId param — use endTime of last trade in previous page
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from core.reconciliation.hibachi import HibachiReconciler


ACCOUNT_ID = 22919


def _trade(i, ts):
    return {
        "id": i, "side": "Buy", "liquidity": "TAKER",
        "symbol": "SUI/USDT-P", "bidAccountId": ACCOUNT_ID, "askAccountId": 9999,
        "bidOrderId": 1, "askOrderId": 2,
        "fee": "0.02", "price": "1.0", "quantity": "10",
        "realizedPnl": "0", "is_taker": True, "orderType": "MARKET",
        "timestamp": ts, "timestamp_ns_partial": 0,
    }


@pytest.mark.asyncio
async def test_paginates_when_window_exceeds_single_page():
    # 3 pages of 500 trades each, oldest trades last
    now_ts = int(datetime.now(timezone.utc).timestamp())
    page1 = [_trade(3000 + i, now_ts - i * 60) for i in range(500)]       # 0-500 min ago
    page2 = [_trade(2000 + i, now_ts - (500 + i) * 60) for i in range(500)]  # 500-1000 min ago
    page3 = [_trade(1000 + i, now_ts - (1000 + i) * 60) for i in range(100)]  # 1000-1100 min ago

    sdk = MagicMock()
    sdk.get_account_id = MagicMock(return_value=ACCOUNT_ID)
    sdk.get_balance = AsyncMock(return_value=50.0)
    sdk.get_positions = AsyncMock(return_value=[])

    call_log = []
    async def fake_request(method, path, params=None):
        call_log.append(params)
        end = (params or {}).get("endTime")
        if end is None:
            return {"trades": page1}
        end = int(end)
        if end > now_ts - 500 * 60:
            return {"trades": page2}
        if end > now_ts - 1000 * 60:
            return {"trades": page3}
        return {"trades": []}
    sdk._request = AsyncMock(side_effect=fake_request)

    r = HibachiReconciler(sdk=sdk)
    since = datetime.fromtimestamp(now_ts - 1200 * 60, tz=timezone.utc)  # 20h ago
    snap = await r.snapshot(since=since)

    # Expect 1100 unique fills fetched
    assert len(snap.new_fills) == 1100, f"got {len(snap.new_fills)}"
    # Expect at least 3 pagination calls
    assert len(call_log) >= 3


@pytest.mark.asyncio
async def test_stops_paginating_when_since_reached():
    """If since=5min ago, one page should be enough."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    page1 = [_trade(3000 + i, now_ts - i * 60) for i in range(500)]

    call_log = []
    async def fake_request(method, path, params=None):
        call_log.append(params)
        return {"trades": page1}

    sdk = MagicMock()
    sdk.get_account_id = MagicMock(return_value=ACCOUNT_ID)
    sdk.get_balance = AsyncMock(return_value=50.0)
    sdk.get_positions = AsyncMock(return_value=[])
    sdk._request = AsyncMock(side_effect=fake_request)

    r = HibachiReconciler(sdk=sdk)
    since = datetime.fromtimestamp(now_ts - 10 * 60, tz=timezone.utc)  # 10 min ago
    snap = await r.snapshot(since=since)

    # Should only fetch one page (oldest in page1 is 500min ago < since boundary? NO — since=10min, page1 goes 0-500min back so extends past since)
    # Key invariant: once we see a trade older than `since`, stop paginating
    assert len(call_log) == 1
    # And only return the ones within window
    assert all(f.ts >= since for f in snap.new_fills)


@pytest.mark.asyncio
async def test_no_infinite_loop_when_endpoint_returns_same_data():
    """Defensive: if the server returns the same trades forever, cap at 20 pages."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    page = [_trade(9000 + i, now_ts - i * 60) for i in range(500)]

    call_log = []
    async def fake_request(method, path, params=None):
        call_log.append(params)
        return {"trades": page}

    sdk = MagicMock()
    sdk.get_account_id = MagicMock(return_value=ACCOUNT_ID)
    sdk.get_balance = AsyncMock(return_value=50.0)
    sdk.get_positions = AsyncMock(return_value=[])
    sdk._request = AsyncMock(side_effect=fake_request)

    r = HibachiReconciler(sdk=sdk)
    since = datetime.fromtimestamp(now_ts - 99999 * 60, tz=timezone.utc)  # 69 days back
    snap = await r.snapshot(since=since)
    # Must not hang — cap at 20 pages
    assert len(call_log) <= 20
