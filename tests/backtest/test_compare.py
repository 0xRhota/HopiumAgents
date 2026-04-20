import pytest
from datetime import datetime, timezone

from core.backtest.compare import compare_pnl, ComparisonResult
from core.reconciliation.base import Fill


def _fill(realized=None, fee=0.05, opens="OPEN"):
    return Fill(
        exchange="nado", symbol="LIT-PERP", fill_id="x", order_id="x",
        ts=datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
        side="BUY", size=100.0, price=1.0, fee=fee, is_maker=True,
        realized_pnl_usd=realized, opens_or_closes=opens,
    )


def test_within_tolerance_passes():
    sim = [_fill(realized=1.0, opens="CLOSE")]
    live = [_fill(realized=1.05, opens="CLOSE")]
    r = compare_pnl(sim, live, tolerance_usd=1.0, tolerance_pct=0.05)
    assert isinstance(r, ComparisonResult)
    assert r.passed is True


def test_beyond_tolerance_fails():
    sim = [_fill(realized=10.0, opens="CLOSE")]
    live = [_fill(realized=2.0, opens="CLOSE")]
    r = compare_pnl(sim, live, tolerance_usd=1.0, tolerance_pct=0.05)
    assert r.passed is False
    assert r.divergence_usd == pytest.approx(8.0)


def test_reports_per_field_breakdown():
    sim = [_fill(realized=1.0, fee=0.05, opens="CLOSE")]
    live = [_fill(realized=1.0, fee=0.10, opens="CLOSE")]
    r = compare_pnl(sim, live, tolerance_usd=0.01, tolerance_pct=0.001)
    assert r.sim_fees == pytest.approx(0.05)
    assert r.live_fees == pytest.approx(0.10)
    assert "fee" in r.notes.lower()
