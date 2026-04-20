import pytest
from datetime import datetime, timezone

from core.backtest.kelly import kelly_fraction
from core.reconciliation.base import Fill


def _close(realized, fee=0.0):
    return Fill(
        exchange="nado", symbol="X", fill_id="x", order_id="x",
        ts=datetime(2026, 4, 18, tzinfo=timezone.utc),
        side="SELL", size=1, price=1.0, fee=fee, is_maker=True,
        realized_pnl_usd=realized, opens_or_closes="CLOSE",
    )


def test_zero_when_no_wins():
    assert kelly_fraction([_close(-1.0), _close(-2.0)]) == 0.0


def test_zero_when_no_losses():
    assert kelly_fraction([_close(1.0), _close(2.0)]) == 0.0


def test_simple_case_60pct_wr_2x_payoff():
    # 60% WR, avg_win=$2, avg_loss=$1 → b=2, p=0.6 → kelly=(0.6*2 - 0.4)/2 = 0.4
    fills = [_close(2.0)] * 6 + [_close(-1.0)] * 4
    assert kelly_fraction(fills) == pytest.approx(0.4, abs=1e-3)


def test_clipped_to_max_one():
    fills = [_close(10.0)] * 9 + [_close(-1.0)]
    assert kelly_fraction(fills) <= 1.0
