"""Tests for HibachiReconciler.

Schema verified via live probe 2026-04-17 at GET /trade/account/trades.

Hibachi conventions (different from Nado/Paradex):
- side = "Buy" | "Sell" (mixed case, not "BUY"/"SELL")
- realizedPnl = "0.000000" string for opening, nonzero for closing
- fee already in USD units (no x18 scaling)
- timestamp = seconds epoch
- bidAccountId/askAccountId: our fill was on whichever side our accountId matches
- bidOrderId/askOrderId: pick whichever matches our side
- is_taker: bool
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from core.reconciliation.base import Fill, Position
from core.reconciliation.hibachi import HibachiReconciler


ACCOUNT_ID = 22919

SAMPLE_TRADES = {
    "trades": [
        # TAKER Buy, closing (realizedPnl != 0)
        {
            "askAccountId": 26813,
            "askOrderId": 595428888557650944,
            "bidAccountId": ACCOUNT_ID,
            "bidOrderId": 595428891860665344,
            "fee": "0.017796",
            "id": 507214860,
            "is_taker": True,
            "orderType": "MARKET",
            "price": "0.993711421",
            "quantity": "39.798187",
            "realizedPnl": "-0.190092",
            "side": "Buy",
            "symbol": "SUI/USDT-P",
            "timestamp": 1776424910,
            "timestamp_ns_partial": 113000000,
        },
        # MAKER Sell, opening (realizedPnl = 0)
        {
            "askAccountId": ACCOUNT_ID,
            "askOrderId": 595393279503107072,
            "bidAccountId": 26822,
            "bidOrderId": 595393303803330560,
            "fee": "0.000000",
            "id": 506066602,
            "is_taker": False,
            "orderType": "LIMIT",
            "price": "84.5897000",
            "quantity": "0.48398400",
            "realizedPnl": "0.000000",
            "side": "Sell",
            "symbol": "SOL/USDT-P",
            "timestamp": 1776289152,
            "timestamp_ns_partial": 454000000,
        },
    ]
}


SAMPLE_BALANCE = 23.645336


SAMPLE_POSITIONS = [
    {
        "symbol": "BTC/USDT-P",
        "side": "LONG",
        "amount": 0.001,
        "openPrice": 74000.0,
        "unrealizedTradingPnl": 0.5,
    }
]


@pytest.fixture
def mock_sdk():
    sdk = MagicMock()
    sdk.get_account_id = MagicMock(return_value=ACCOUNT_ID)
    sdk._request = AsyncMock(return_value=SAMPLE_TRADES)
    sdk.get_balance = AsyncMock(return_value=SAMPLE_BALANCE)
    sdk.get_positions = AsyncMock(return_value=SAMPLE_POSITIONS)
    return sdk


def test_exchange_name(mock_sdk):
    r = HibachiReconciler(sdk=mock_sdk)
    assert r.exchange == "hibachi"


# ── snapshot ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_equity(mock_sdk):
    r = HibachiReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    assert snap.equity == pytest.approx(23.645336)


@pytest.mark.asyncio
async def test_snapshot_positions(mock_sdk):
    r = HibachiReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    assert len(snap.positions) == 1
    p = snap.positions[0]
    assert p.symbol == "BTC/USDT-P"
    assert p.side == "LONG"
    assert p.entry_price == pytest.approx(74000.0)
    assert p.unrealized_pnl == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_snapshot_fills_normalize_side(mock_sdk):
    """Hibachi uses 'Buy'/'Sell' — must normalize to 'BUY'/'SELL'."""
    r = HibachiReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    sides = {f.side for f in snap.new_fills}
    assert sides == {"BUY", "SELL"}


@pytest.mark.asyncio
async def test_snapshot_fill_fee_positive(mock_sdk):
    """Hibachi taker fees are strings like '0.017796' — parse to positive float."""
    r = HibachiReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    taker_fill = next(f for f in snap.new_fills if not f.is_maker)
    assert taker_fill.fee == pytest.approx(0.017796)


@pytest.mark.asyncio
async def test_snapshot_fill_maker_detection(mock_sdk):
    """is_taker=False → is_maker=True."""
    r = HibachiReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    maker = next(f for f in snap.new_fills if f.is_maker)
    assert maker.is_maker is True
    assert maker.fee == pytest.approx(0.0)  # Hibachi has 0% maker fee


@pytest.mark.asyncio
async def test_snapshot_fill_open_close(mock_sdk):
    r = HibachiReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    # realizedPnl != 0 → CLOSE, == 0 → OPEN
    closes = [f for f in snap.new_fills if f.opens_or_closes == "CLOSE"]
    opens = [f for f in snap.new_fills if f.opens_or_closes == "OPEN"]
    assert len(closes) == 1
    assert len(opens) == 1
    assert closes[0].realized_pnl_usd == pytest.approx(-0.190092)
    assert opens[0].realized_pnl_usd is None


@pytest.mark.asyncio
async def test_snapshot_fill_id_uses_hibachi_id(mock_sdk):
    r = HibachiReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    fill_ids = {f.fill_id for f in snap.new_fills}
    assert "507214860" in fill_ids
    assert "506066602" in fill_ids


@pytest.mark.asyncio
async def test_snapshot_fill_order_id_matches_our_side(mock_sdk):
    """For a Buy trade, we're the bidder (bidAccountId = ours)."""
    r = HibachiReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    buy_fill = next(f for f in snap.new_fills if f.side == "BUY")
    assert buy_fill.order_id == "595428891860665344"  # bidOrderId for a Buy
    sell_fill = next(f for f in snap.new_fills if f.side == "SELL")
    assert sell_fill.order_id == "595393279503107072"  # askOrderId for a Sell


@pytest.mark.asyncio
async def test_snapshot_since_filter(mock_sdk):
    r = HibachiReconciler(sdk=mock_sdk)
    # Trades have ts 1776424910 and 1776289152
    cutoff = datetime.fromtimestamp(1776300000, tz=timezone.utc)
    snap = await r.snapshot(since=cutoff)
    assert len(snap.new_fills) == 1
    assert snap.new_fills[0].fill_id == "507214860"


@pytest.mark.asyncio
async def test_pnl_window_aggregates_fills(mock_sdk):
    r = HibachiReconciler(sdk=mock_sdk)
    w = await r.get_pnl_window(hours=999)  # include both sample fills
    assert w.realized_pnl == pytest.approx(-0.190092)  # only the non-zero
    assert w.fees_paid == pytest.approx(0.017796)
    assert w.trade_count == 2
