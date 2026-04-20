"""Tests for ParadexReconciler.

These tests use mocked Paradex client responses based on REAL schema
probed live on 2026-04-17. Do not guess fields — schemas are copied
from actual API responses.

Key Paradex conventions:
- side = "BUY" | "SELL"
- liquidity = "TAKER" | "MAKER"
- created_at = milliseconds epoch (int)
- fee currency is USDC (already USD-denominated)
- realized_pnl = price-only PnL attributable to this fill, GROSS of the fee on this fill
- realized_funding = funding attributable to this fill (signed)
- size in base asset, price in quote (USD)
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from core.reconciliation.base import ExchangeSnapshot, Fill, Position
from core.reconciliation.paradex import ParadexReconciler


# Real account summary (from live probe 2026-04-17)
LIVE_ACCOUNT = MagicMock()
LIVE_ACCOUNT.account_value = "27.66004784"


LIVE_POSITIONS_RESPONSE = {
    "results": [
        {
            "id": "acc-BTC-USD-PERP",
            "account": "0x614a",
            "market": "BTC-USD-PERP",
            "status": "OPEN",
            "side": "LONG",
            "size": "0.001",
            "average_entry_price": "74689.9",
            "average_entry_price_usd": "74689.9",
            "unrealized_pnl": "-0.05",
            "unrealized_funding_pnl": "0.0009",
            "cost": "74.6899",
            "leverage": "10",
            "last_updated_at": 1776389555378,
        },
        {
            "id": "acc-ASTER-USD-PERP",
            "account": "0x614a",
            "market": "ASTER-USD-PERP",
            "status": "CLOSED",
            "side": "LONG",
            "size": "0",
            "average_entry_price": "0",
            "unrealized_pnl": "0",
        },
    ]
}


LIVE_FILLS_RESPONSE = {
    "results": [
        {
            "id": "fill-1",
            "side": "BUY",
            "liquidity": "TAKER",
            "market": "BTC-USD-PERP",
            "order_id": "ord-1",
            "price": "74689.9",
            "size": "0.00014",
            "fee": "0.0020916326990885",
            "fee_currency": "USDC",
            "created_at": 1776389555378,  # 2026-04-17 something
            "realized_pnl": "0",  # opening
            "realized_funding": "0",
            "fill_type": "FILL",
        },
        {
            "id": "fill-2",
            "side": "SELL",
            "liquidity": "MAKER",
            "market": "BTC-USD-PERP",
            "order_id": "ord-2",
            "price": "74790.0",
            "size": "0.00014",
            "fee": "-0.001045816",  # maker rebate on Paradex
            "fee_currency": "USDC",
            "created_at": 1776389655378,
            "realized_pnl": "0.014014",  # (74790 - 74689.9) * 0.00014
            "realized_funding": "0.0000883293774",
            "fill_type": "FILL",
        },
    ]
}


@pytest.fixture
def mock_client():
    """Build a mock ParadexSubkey.api_client."""
    client = MagicMock()
    client.api_client.fetch_account_summary = MagicMock(return_value=LIVE_ACCOUNT)
    client.api_client.fetch_positions = MagicMock(return_value=LIVE_POSITIONS_RESPONSE)
    client.api_client.fetch_fills = MagicMock(return_value=LIVE_FILLS_RESPONSE)
    return client


# ── Identity / basic ────────────────────────────────────────────────

def test_exchange_name(mock_client):
    r = ParadexReconciler(client=mock_client)
    assert r.exchange == "paradex"


# ── snapshot() ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_returns_equity(mock_client):
    r = ParadexReconciler(client=mock_client)
    snap = await r.snapshot()
    assert snap.exchange == "paradex"
    assert snap.equity == pytest.approx(27.66004784)


@pytest.mark.asyncio
async def test_snapshot_filters_closed_positions(mock_client):
    """Positions with status=CLOSED should not appear in snapshot.positions."""
    r = ParadexReconciler(client=mock_client)
    snap = await r.snapshot()
    assert len(snap.positions) == 1
    assert snap.positions[0].symbol == "BTC-USD-PERP"
    assert snap.positions[0].side == "LONG"


@pytest.mark.asyncio
async def test_snapshot_position_fields(mock_client):
    r = ParadexReconciler(client=mock_client)
    snap = await r.snapshot()
    p = snap.positions[0]
    assert p.size == pytest.approx(0.001)
    assert p.entry_price == pytest.approx(74689.9)
    assert p.unrealized_pnl == pytest.approx(-0.05)
    assert p.funding_accrued == pytest.approx(-0.0009)  # negative=received, positive paradex reports =received


@pytest.mark.asyncio
async def test_snapshot_fills_mapped_correctly(mock_client):
    r = ParadexReconciler(client=mock_client)
    snap = await r.snapshot()
    assert len(snap.new_fills) == 2

    # Opening fill — BUY, TAKER, positive fee
    f1 = next(f for f in snap.new_fills if f.fill_id == "fill-1")
    assert f1.side == "BUY"
    assert f1.is_maker is False
    assert f1.fee == pytest.approx(0.0020916326990885)
    assert f1.realized_pnl_usd is None  # Paradex returns "0" for opening; we translate to None
    assert f1.opens_or_closes == "OPEN"

    # Closing fill — SELL, MAKER, negative fee (rebate)
    f2 = next(f for f in snap.new_fills if f.fill_id == "fill-2")
    assert f2.side == "SELL"
    assert f2.is_maker is True
    assert f2.fee == pytest.approx(-0.001045816)
    assert f2.realized_pnl_usd == pytest.approx(0.014014)
    assert f2.opens_or_closes == "CLOSE"


@pytest.mark.asyncio
async def test_snapshot_since_filter(mock_client):
    """Only fills with ts >= `since` should be returned."""
    r = ParadexReconciler(client=mock_client)
    cutoff = datetime(2026, 4, 17, 0, 0, 59, tzinfo=timezone.utc)
    # Both fills have created_at ~ 1776389555378ms and 1776389655378ms
    # 1776389555378ms = 2026-04-17 ~03:19:15 UTC  — both should pass this early cutoff
    snap = await r.snapshot(since=cutoff)
    assert len(snap.new_fills) == 2

    # Only 2nd fill
    cutoff2 = datetime.fromtimestamp(1776389600, tz=timezone.utc)  # between the two
    snap2 = await r.snapshot(since=cutoff2)
    assert len(snap2.new_fills) == 1
    assert snap2.new_fills[0].fill_id == "fill-2"


@pytest.mark.asyncio
async def test_snapshot_ts_is_utc(mock_client):
    r = ParadexReconciler(client=mock_client)
    snap = await r.snapshot()
    assert snap.ts.tzinfo is not None
    for f in snap.new_fills:
        assert f.ts.tzinfo is not None


# ── get_pnl_window() ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pnl_window_sums_fills(mock_client):
    r = ParadexReconciler(client=mock_client)
    # Large window so fills fixture stays in range as time passes
    w = await r.get_pnl_window(hours=24 * 365)
    # realized: 0 + 0.014014 = 0.014014
    # fees: 0.00209 + (-0.001046) = 0.001046 paid
    # funding: 0 + 0.0000883 = 0.0000883 received (paradex sign: positive = received)
    assert w.realized_pnl == pytest.approx(0.014014)
    assert w.fees_paid == pytest.approx(0.0020916326990885 - 0.001045816)
    # Sign convention in WindowPnL: funding_paid positive = paid. Paradex gives positive = received.
    # So we should invert when mapping.
    assert w.funding_paid == pytest.approx(-0.0000883293774)
    assert w.trade_count == 2


# ── OPEN/CLOSE classification ────────────────────────────────────────

@pytest.mark.asyncio
async def test_open_close_classified_by_realized_pnl(mock_client):
    """A fill with realized_pnl==0 is OPEN; nonzero is CLOSE.

    This matches Paradex convention: the entry fill has 0 realized,
    the exit fill carries the PnL.
    """
    r = ParadexReconciler(client=mock_client)
    snap = await r.snapshot()
    open_fills = [f for f in snap.new_fills if f.opens_or_closes == "OPEN"]
    close_fills = [f for f in snap.new_fills if f.opens_or_closes == "CLOSE"]
    assert len(open_fills) == 1
    assert len(close_fills) == 1
