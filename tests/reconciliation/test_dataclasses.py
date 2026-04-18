"""Tests for reconciliation dataclasses.

Dataclasses must:
- be immutable (frozen) to prevent accidental mutation after reconciler emits them
- serialize to/from JSON losslessly
- validate numeric sign conventions (fees positive = paid, negative = rebate)
"""

import pytest
from datetime import datetime, timezone
from core.reconciliation.base import (
    ExchangeSnapshot,
    Fill,
    Position,
    WindowPnL,
)


# ── Fill ─────────────────────────────────────────────────────────────

def test_fill_required_fields():
    f = Fill(
        exchange="nado",
        symbol="LIT-PERP",
        fill_id="0xabc",
        order_id="0xabc",
        ts=datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
        side="BUY",
        size=100.0,
        price=1.02,
        fee=0.04,
        is_maker=True,
        realized_pnl_usd=None,
        opens_or_closes="OPEN",
    )
    assert f.symbol == "LIT-PERP"
    assert f.notional_usd == pytest.approx(102.0)


def test_fill_notional_for_short():
    f = Fill(
        exchange="nado",
        symbol="ZEC-PERP",
        fill_id="0xdef",
        order_id="0xdef",
        ts=datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
        side="SELL",
        size=0.5,
        price=330.0,
        fee=0.058,
        is_maker=False,
        realized_pnl_usd=None,
        opens_or_closes="OPEN",
    )
    assert f.notional_usd == pytest.approx(165.0)


def test_fill_is_frozen():
    f = Fill(
        exchange="nado", symbol="X", fill_id="1", order_id="1",
        ts=datetime.now(timezone.utc), side="BUY", size=1, price=1, fee=0,
        is_maker=True, realized_pnl_usd=None, opens_or_closes="OPEN",
    )
    with pytest.raises((AttributeError, TypeError)):
        f.price = 999


def test_fill_to_from_dict_roundtrip():
    f = Fill(
        exchange="paradex", symbol="BTC-USD-PERP", fill_id="fill-123", order_id="ord-456",
        ts=datetime(2026, 4, 17, 15, 30, 0, tzinfo=timezone.utc),
        side="BUY", size=0.001, price=74000.0, fee=-0.007,  # rebate
        is_maker=True, realized_pnl_usd=None, opens_or_closes="OPEN",
    )
    d = f.to_dict()
    f2 = Fill.from_dict(d)
    assert f2 == f


def test_fill_rebate_has_negative_fee():
    """Paradex maker gets rebates → fee should be negative."""
    f = Fill(
        exchange="paradex", symbol="BTC-USD-PERP", fill_id="1", order_id="1",
        ts=datetime.now(timezone.utc), side="BUY", size=0.001, price=74000.0,
        fee=-0.007, is_maker=True, realized_pnl_usd=None, opens_or_closes="OPEN",
    )
    assert f.fee < 0
    # Verify that net effect is a credit
    assert f.effective_cost == pytest.approx(-0.007)


def test_fill_taker_positive_fee():
    """Taker pays fee → positive."""
    f = Fill(
        exchange="nado", symbol="ETH-PERP", fill_id="1", order_id="1",
        ts=datetime.now(timezone.utc), side="SELL", size=0.04, price=2300,
        fee=0.032, is_maker=False, realized_pnl_usd=None, opens_or_closes="OPEN",
    )
    assert f.fee > 0
    assert f.effective_cost == pytest.approx(0.032)


def test_fill_closing_has_realized_pnl():
    f = Fill(
        exchange="nado", symbol="LIT-PERP", fill_id="2", order_id="2",
        ts=datetime.now(timezone.utc), side="SELL", size=100, price=1.04,
        fee=0.041, is_maker=False, realized_pnl_usd=1.96,  # gross of this fill's fee
        opens_or_closes="CLOSE", linked_entry_fill_id="1",
    )
    assert f.opens_or_closes == "CLOSE"
    assert f.realized_pnl_usd == 1.96
    assert f.linked_entry_fill_id == "1"
    # net = realized - fee
    assert f.net_pnl_usd == pytest.approx(1.96 - 0.041)


def test_fill_opening_has_no_pnl():
    f = Fill(
        exchange="nado", symbol="LIT-PERP", fill_id="1", order_id="1",
        ts=datetime.now(timezone.utc), side="BUY", size=100, price=1.02,
        fee=0.041, is_maker=False, realized_pnl_usd=None, opens_or_closes="OPEN",
    )
    assert f.realized_pnl_usd is None
    assert f.net_pnl_usd is None  # opening fills have no net yet


# ── Position ─────────────────────────────────────────────────────────

def test_position_long():
    p = Position(
        exchange="hibachi", symbol="BTC/USDT-P", side="LONG",
        size=0.001, entry_price=74000.0, unrealized_pnl=0.5,
        funding_accrued=0.0,
    )
    assert p.side == "LONG"
    assert p.notional_usd == pytest.approx(74.0)


def test_position_short():
    p = Position(
        exchange="nado", symbol="SOL-PERP", side="SHORT",
        size=1.2, entry_price=88.0, unrealized_pnl=-0.3,
        funding_accrued=-0.02,  # paid funding
    )
    assert p.side == "SHORT"
    assert p.notional_usd == pytest.approx(105.6)


def test_position_rejects_invalid_side():
    with pytest.raises(ValueError):
        Position(
            exchange="nado", symbol="X", side="sideways",
            size=1, entry_price=1, unrealized_pnl=0, funding_accrued=0,
        )


# ── WindowPnL ────────────────────────────────────────────────────────

def test_window_pnl_net_computed():
    w = WindowPnL(
        exchange="nado",
        window_start=datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
        realized_pnl=-2.92,
        fees_paid=0.23,
        funding_paid=0.0,
        trade_count=10,
    )
    # net = realized - fees + funding(received)
    # funding_paid positive means paid out (cost)
    assert w.net_pnl == pytest.approx(-3.15)


def test_window_pnl_with_funding_received():
    w = WindowPnL(
        exchange="paradex",
        window_start=datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc),
        realized_pnl=1.00,
        fees_paid=-0.10,  # maker rebate → negative
        funding_paid=-0.05,  # received funding → negative
        trade_count=3,
    )
    # net = 1.00 - (-0.10) - (-0.05) = 1.15
    assert w.net_pnl == pytest.approx(1.15)


# ── ExchangeSnapshot ─────────────────────────────────────────────────

def test_snapshot_aggregate_notional():
    ts = datetime.now(timezone.utc)
    pos = [
        Position("nado", "LIT-PERP", "LONG", 100, 1.02, 0.5, 0.0),
        Position("nado", "ZEC-PERP", "SHORT", 0.3, 330, -0.2, -0.01),
    ]
    snap = ExchangeSnapshot(
        exchange="nado", ts=ts, equity=55.0, positions=pos,
        new_fills=[], funding_paid_since=0.0,
    )
    assert snap.total_notional == pytest.approx(201.0)  # 102 + 99
    assert snap.total_unrealized == pytest.approx(0.3)


def test_snapshot_empty_ok():
    snap = ExchangeSnapshot(
        exchange="hibachi", ts=datetime.now(timezone.utc),
        equity=23.65, positions=[], new_fills=[], funding_paid_since=0.0,
    )
    assert snap.total_notional == 0.0
    assert snap.total_unrealized == 0.0
