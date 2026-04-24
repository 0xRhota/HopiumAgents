"""Verify HibachiAdapter and NadoAdapter close_position() NEVER calls
create_market_order(). User directive 2026-04-20: no taker fallback.

If an engineer re-adds market fallback, these tests fail and block the PR.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_hibachi_close_never_calls_market_order():
    from core.strategies.momentum.exchange_adapter import HibachiAdapter

    adapter = HibachiAdapter.__new__(HibachiAdapter)
    adapter.sdk = MagicMock()
    adapter.sdk.get_price = AsyncMock(return_value=70000.0)
    adapter.sdk.create_limit_order = AsyncMock(return_value={"status": "success"})
    adapter.sdk.create_market_order = AsyncMock(return_value={"status": "success"})
    adapter.sdk.cancel_all_orders = AsyncMock(return_value=True)

    # Simulate: position exists; limit never fills
    call_count = {"n": 0}
    async def get_position_loop(sym):
        call_count["n"] += 1
        return {"side": "LONG", "size": 0.001, "entry_price": 70000.0}
    adapter.get_position = get_position_loop

    result = await adapter.close_position("BTC/USDT-P")
    assert result is False  # never filled
    adapter.sdk.create_market_order.assert_not_called()
    assert adapter.sdk.create_limit_order.call_count >= 1


@pytest.mark.asyncio
async def test_hibachi_close_succeeds_when_limit_fills():
    from core.strategies.momentum.exchange_adapter import HibachiAdapter

    adapter = HibachiAdapter.__new__(HibachiAdapter)
    adapter.sdk = MagicMock()
    adapter.sdk.get_price = AsyncMock(return_value=70000.0)
    adapter.sdk.create_limit_order = AsyncMock(return_value={"status": "success"})
    adapter.sdk.create_market_order = AsyncMock(return_value={"status": "success"})
    adapter.sdk.cancel_all_orders = AsyncMock(return_value=True)

    # First get_position (outer check): position exists
    # After that: position gone (simulating fill)
    call_count = {"n": 0}
    async def get_position_fills(sym):
        call_count["n"] += 1
        if call_count["n"] <= 1:
            return {"side": "LONG", "size": 0.001, "entry_price": 70000.0}
        return None
    adapter.get_position = get_position_fills

    result = await adapter.close_position("BTC/USDT-P")
    assert result is True
    adapter.sdk.create_market_order.assert_not_called()


@pytest.mark.asyncio
async def test_nado_close_no_market_within_grace_window():
    """Within the 15-min stuck-grace window, NO market order is allowed.
    (After the window, the safety valve may unstick — covered separately.)"""
    from core.strategies.momentum.exchange_adapter import NadoAdapter

    adapter = NadoAdapter.__new__(NadoAdapter)
    adapter.sdk = MagicMock()
    adapter.sdk.get_product_by_symbol = AsyncMock(return_value={
        "oracle_price": 1.0, "price_increment": 0.0001,
        "size_increment": 0.1, "min_notional": 100.0,
    })
    adapter.sdk.create_limit_order = AsyncMock(return_value={"status": "success"})
    adapter.sdk.create_market_order = AsyncMock(return_value={"status": "success"})
    adapter.sdk.cancel_all_orders = AsyncMock(return_value=True)
    adapter.cancel_all = AsyncMock(return_value=1)
    adapter._stuck_first_attempt_ts = {}
    adapter.STUCK_GRACE_MIN = 15.0

    async def get_position_loop(sym):
        return {"side": "LONG", "size": 100.0, "entry_price": 1.0}
    adapter.get_position = get_position_loop

    result = await adapter.close_position("LIT-PERP")
    assert result is False
    # First attempt within grace — no market order yet
    adapter.sdk.create_market_order.assert_not_called()


@pytest.mark.asyncio
async def test_nado_stuck_safety_valve_fires_after_grace():
    """After >STUCK_GRACE_MIN of failed maker closes on same symbol,
    safety valve issues one market order to unstick. User directive 2026-04-24."""
    from core.strategies.momentum.exchange_adapter import NadoAdapter
    import time

    adapter = NadoAdapter.__new__(NadoAdapter)
    adapter.sdk = MagicMock()
    adapter.sdk.get_product_by_symbol = AsyncMock(return_value={
        "oracle_price": 1.0, "price_increment": 0.0001,
        "size_increment": 0.1, "min_notional": 100.0,
    })
    adapter.sdk.create_limit_order = AsyncMock(return_value={"status": "success"})
    adapter.sdk.create_market_order = AsyncMock(return_value={"status": "success"})
    adapter.sdk.cancel_all_orders = AsyncMock(return_value=True)
    adapter.cancel_all = AsyncMock(return_value=1)
    # Preload stuck state — pretend this symbol has been failing for 20min
    adapter._stuck_first_attempt_ts = {"LIT-PERP": time.time() - 20 * 60}
    adapter.STUCK_GRACE_MIN = 15.0

    async def get_position_loop(sym):
        return {"side": "LONG", "size": 100.0, "entry_price": 1.0}
    adapter.get_position = get_position_loop

    result = await adapter.close_position("LIT-PERP")
    # Safety valve: market order fires once, return True
    assert result is True
    adapter.sdk.create_market_order.assert_called_once()
    # Stuck state cleared
    assert "LIT-PERP" not in adapter._stuck_first_attempt_ts
