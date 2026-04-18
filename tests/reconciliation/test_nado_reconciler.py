"""Tests for NadoReconciler.

Schema from live Nado Archive API probe 2026-04-17.

Key Nado conventions:
- Numbers are x18 scaled (divide by 10^18 for USD / human units)
- base_filled signed: negative = sell
- `is_taker` bool on match
- product_id integer → needs PRODUCT_SYMBOLS map
- timestamps come from the linked tx (via submission_idx)
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from core.reconciliation.base import ExchangeSnapshot, Fill
from core.reconciliation.nado import NadoReconciler


SAMPLE_ARCHIVE_RESPONSE = {
    "matches": [
        # SELL match (negative base_filled) — closes a LONG
        {
            "digest": "0x273449e718c2af65a48b46f7fafd8b66d005828bb6b64944fbe2442b9c023288",
            "order": {
                "sender": "0x49d69c93...",
                "priceX18": "3193000000000000",  # $0.003193
                "amount": "-15600000000000000000000",
                "expiration": "1776427807",
                "nonce": "1862719281642006487",
            },
            "base_filled": "-15600000000000000000000",  # -15600 (kBONK units)
            "quote_filled": "99680200000000000000",     # $99.68
            "fee": "35000000000000000",                 # $0.035
            "is_taker": True,
            "realized_pnl": "-343200000000000000",      # -$0.343
            "pre_balance": {"base": {"perp": {"product_id": 56, "balance": {}}}},
            "post_balance": {"base": {"perp": {"product_id": 56, "balance": {}}}},
            "submission_idx": "46264741",
        },
        # BUY match (positive base_filled) — opens a LONG on TAO
        {
            "digest": "0x8c7efb8bfd0d32a8ae403c4711aec331ac247309b8bfff74886891eb0dec0b16",
            "order": {
                "sender": "0x49d69c93...",
                "priceX18": "176420000000000000000",  # $176.42 (wrong scale here; would be per-unit)
                "amount": "1200000000000000000",
                "expiration": "1776427177",
            },
            "base_filled": "1200000000000000000",       # 1.2 TAO
            "quote_filled": "-105877056969939478817",   # -$105.88 (quote flows out for a buy)
            "fee": "37056969939478817",                 # $0.037
            "is_taker": True,
            "realized_pnl": "0",                        # opening
            "pre_balance": {"base": {"perp": {"product_id": 8, "balance": {}}}},
            "post_balance": {"base": {"perp": {"product_id": 8, "balance": {}}}},
            "submission_idx": "46264700",
        },
    ],
    "txs": [
        {"submission_idx": "46264741", "timestamp": "1776427800"},
        {"submission_idx": "46264700", "timestamp": "1776427100"},
    ],
}


SAMPLE_GET_PNL_24H = {
    "hours": 24,
    "trade_count": 20,
    "realized_pnl": -2.9244,
    "fees": 0.2298,
    "net_pnl": -3.1542,
}


# Real Nado API returns x18-scaled int strings for healths & v_quote_balance
SAMPLE_SUBACCOUNT_INFO = {
    "healths": [
        {"assets": "55125400000000000000", "liabilities": "0"},
        {"assets": "54872300000000000000", "liabilities": "0"},
        {"assets": "55130000000000000000", "liabilities": "0"},  # $55.13
    ]
}


SAMPLE_POSITIONS = [
    {
        "symbol": "LIT-PERP",
        "product_id": 56,
        "amount_float": 100.0,
        "v_quote_balance": "-102300000000000000000",  # -$102.3 x18
    },
    {
        "symbol": "SOL-PERP",
        "product_id": 8,
        "amount_float": -1.2,  # SHORT
        "v_quote_balance": "105000000000000000000",   # $105.0 x18
    },
]


@pytest.fixture
def mock_sdk():
    sdk = MagicMock()
    sdk.PRODUCT_SYMBOLS = {56: "LIT", 8: "TAO"}
    sdk._get_subaccount_bytes32 = MagicMock(return_value="0xsub")
    sdk._from_x18 = lambda v: float(v) / 1e18 if v else 0.0
    sdk._archive_query = AsyncMock(return_value=SAMPLE_ARCHIVE_RESPONSE)
    sdk.get_pnl = AsyncMock(return_value=SAMPLE_GET_PNL_24H)
    sdk.get_subaccount_info = AsyncMock(return_value=SAMPLE_SUBACCOUNT_INFO)
    sdk.get_positions = AsyncMock(return_value=SAMPLE_POSITIONS)
    return sdk


# ── Basic ────────────────────────────────────────────────────────────

def test_exchange_name(mock_sdk):
    r = NadoReconciler(sdk=mock_sdk)
    assert r.exchange == "nado"


# ── snapshot ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_snapshot_equity_from_healths(mock_sdk):
    r = NadoReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    assert snap.equity == pytest.approx(55.13)


@pytest.mark.asyncio
async def test_snapshot_positions_mapped(mock_sdk):
    r = NadoReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    assert len(snap.positions) == 2
    lit = next(p for p in snap.positions if p.symbol == "LIT-PERP")
    assert lit.side == "LONG"
    assert lit.size == pytest.approx(100.0)
    sol = next(p for p in snap.positions if p.symbol == "SOL-PERP")
    assert sol.side == "SHORT"
    assert sol.size == pytest.approx(1.2)


@pytest.mark.asyncio
async def test_snapshot_fills_from_archive(mock_sdk):
    r = NadoReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    assert len(snap.new_fills) == 2

    # The SELL fill (realized_pnl != 0 → CLOSE)
    close_fill = next(f for f in snap.new_fills if f.opens_or_closes == "CLOSE")
    assert close_fill.side == "SELL"
    assert close_fill.is_maker is False
    assert close_fill.realized_pnl_usd == pytest.approx(-0.3432)
    assert close_fill.fee == pytest.approx(0.035)

    # The BUY fill (realized_pnl == 0 → OPEN)
    open_fill = next(f for f in snap.new_fills if f.opens_or_closes == "OPEN")
    assert open_fill.side == "BUY"
    assert open_fill.realized_pnl_usd is None


@pytest.mark.asyncio
async def test_snapshot_fill_ts_from_tx(mock_sdk):
    r = NadoReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    # tx 46264741 → ts 1776427800 (2026-...)
    close_fill = next(f for f in snap.new_fills if f.fill_id.startswith("0x273449"))
    expected = datetime.fromtimestamp(1776427800, tz=timezone.utc)
    assert close_fill.ts == expected


@pytest.mark.asyncio
async def test_snapshot_since_filter(mock_sdk):
    r = NadoReconciler(sdk=mock_sdk)
    # The two txs have ts 1776427100 and 1776427800
    cutoff = datetime.fromtimestamp(1776427500, tz=timezone.utc)
    snap = await r.snapshot(since=cutoff)
    assert len(snap.new_fills) == 1
    assert snap.new_fills[0].ts >= cutoff


# ── get_pnl_window ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pnl_window_uses_sdk_get_pnl(mock_sdk):
    r = NadoReconciler(sdk=mock_sdk)
    w = await r.get_pnl_window(hours=24)
    assert w.realized_pnl == pytest.approx(-2.9244)
    assert w.fees_paid == pytest.approx(0.2298)
    assert w.trade_count == 20
    # net_pnl = realized - fees - funding(=0 since not returned)
    assert w.net_pnl == pytest.approx(-3.1542)


# ── maker-only detection ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_is_maker_inverted_from_is_taker(mock_sdk):
    """Nado archive reports is_taker. We store is_maker (opposite)."""
    r = NadoReconciler(sdk=mock_sdk)
    snap = await r.snapshot()
    # Both sample fills have is_taker=True
    assert all(f.is_maker is False for f in snap.new_fills)
