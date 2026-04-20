import pytest
from core.backtest.exchange_sim import (
    simulate_order, ExchangeSpec, NADO, HIBACHI, PARADEX,
)


def test_nado_taker_market_buy_fee():
    fill = simulate_order(NADO, side="BUY", size=100.0, price=1.0, mid=1.0,
                          half_spread=0.0001, post_only=False)
    assert fill is not None
    assert fill["is_maker"] is False
    # 3.5 bps × ~$100 notional = ~$0.035
    assert fill["fee"] == pytest.approx(0.035, abs=1e-3)
    assert fill["fill_price"] > 1.0  # taker pays slippage


def test_nado_post_only_accepts_when_below_bid():
    fill = simulate_order(NADO, side="BUY", size=100.0, price=0.99, mid=1.0,
                          half_spread=0.005, post_only=True)
    assert fill is not None
    assert fill["is_maker"] is True
    # 1 bps × $99 notional
    assert fill["fee"] == pytest.approx(0.0099, abs=1e-6)
    assert fill["fill_price"] == 0.99


def test_nado_post_only_rejects_when_crosses_book():
    fill = simulate_order(NADO, side="BUY", size=100.0, price=1.10, mid=1.0,
                          half_spread=0.005, post_only=True)
    assert fill is None


def test_nado_post_only_sell_rejects_when_below_bid():
    fill = simulate_order(NADO, side="SELL", size=100.0, price=0.90, mid=1.0,
                          half_spread=0.005, post_only=True)
    assert fill is None


def test_paradex_maker_rebate_is_negative_fee():
    fill = simulate_order(PARADEX, side="SELL", size=0.001, price=70010.0,
                          mid=70000.0, half_spread=2.0, post_only=True)
    assert fill is not None
    assert fill["is_maker"] is True
    assert fill["fee"] < 0  # rebate
    # -0.5 bps × $70.01
    assert fill["fee"] == pytest.approx(-0.0035005, abs=1e-6)


def test_hibachi_taker_high_fee():
    fill = simulate_order(HIBACHI, side="SELL", size=0.001, price=70000.0,
                          mid=70000.0, half_spread=1.0, post_only=False)
    assert fill is not None
    assert fill["is_maker"] is False
    # 0.35% × ~$70
    assert fill["fee"] == pytest.approx(0.245, abs=1e-2)


def test_hibachi_rejects_post_only_until_sdk_supports_it():
    fill = simulate_order(HIBACHI, side="BUY", size=0.001, price=69990.0,
                          mid=70000.0, half_spread=1.0, post_only=True)
    assert fill is None


def test_spec_constants_have_expected_values():
    assert NADO.name == "nado"
    assert NADO.maker_fee_bps == 1.0
    assert NADO.taker_fee_bps == 3.5
    assert PARADEX.maker_fee_bps == -0.5
    assert HIBACHI.supports_post_only is False
