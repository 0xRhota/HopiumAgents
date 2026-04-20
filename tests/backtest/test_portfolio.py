import pytest
from datetime import datetime, timezone
from core.backtest.portfolio import Portfolio


def _ts():
    return datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)


def test_portfolio_initializes_with_starting_equity():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    assert p.equity == 100.0
    assert p.leverage == 10.0
    assert p.buying_power == 1000.0
    assert p.positions == {}


def test_open_long_deducts_fee_and_records_position():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("BTC-PERP", "LONG", 0.001, 70000.0,
                    fee=0.07, ts=_ts(), is_maker=False)
    assert "BTC-PERP" in p.positions
    pos = p.positions["BTC-PERP"]
    assert pos["side"] == "LONG"
    assert pos["size"] == 0.001
    assert pos["entry_price"] == 70000.0
    assert p.equity == pytest.approx(99.93)


def test_open_with_maker_rebate_increases_equity():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("BTC-USD-PERP", "LONG", 0.001, 70000.0,
                    fee=-0.0035, ts=_ts(), is_maker=True)
    assert p.equity == pytest.approx(100.0035)


def test_close_long_realizes_pnl_and_deducts_fee():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("BTC-PERP", "LONG", 0.001, 70000.0, fee=0.07, ts=_ts(), is_maker=False)
    p.close_position("BTC-PERP", price=71000.0, fee=0.071, ts=_ts(), is_maker=False)
    assert "BTC-PERP" not in p.positions
    assert p.equity == pytest.approx(100.859)


def test_close_short_realizes_inverted_pnl():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("ETH-PERP", "SHORT", 0.04, 2300.0, fee=0.032, ts=_ts(), is_maker=False)
    p.close_position("ETH-PERP", price=2280.0, fee=0.032, ts=_ts(), is_maker=False)
    assert p.equity == pytest.approx(100.736)


def test_close_unknown_symbol_raises():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    with pytest.raises(KeyError):
        p.close_position("BTC-PERP", price=70000.0, fee=0.07, ts=_ts(), is_maker=False)


def test_open_when_already_in_position_raises():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("BTC-PERP", "LONG", 0.001, 70000.0, fee=0.07, ts=_ts(), is_maker=False)
    with pytest.raises(ValueError):
        p.open_position("BTC-PERP", "LONG", 0.001, 70000.0, fee=0.07, ts=_ts(), is_maker=False)


def test_invalid_side_raises():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    with pytest.raises(ValueError):
        p.open_position("BTC-PERP", "sideways", 0.001, 70000.0, fee=0.07, ts=_ts(), is_maker=False)
