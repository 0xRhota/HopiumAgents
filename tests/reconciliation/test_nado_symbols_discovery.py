"""Nado should fetch symbol mapping from live /symbols endpoint, not hardcode.

Real Nado response (probed 2026-04-18):
  _query("symbols") → {"data": {"symbols": {"MSFT-PERP": {...product_id: 110...}, ...}}}
"""
import pytest
from unittest.mock import AsyncMock, MagicMock


SYMBOLS_RESPONSE = {
    "status": "success",
    "data": {
        "symbols": {
            "BTC-PERP": {"type": "perp", "product_id": 2, "trading_status": "live"},
            "ETH-PERP": {"type": "perp", "product_id": 4, "trading_status": "live"},
            "MSFT-PERP": {"type": "perp", "product_id": 110, "trading_status": "soft_reduce_only", "isolated_only": True},
            "AAPL-PERP": {"type": "perp", "product_id": 96, "trading_status": "live", "isolated_only": True},
            "AMZN-PERP": {"type": "perp", "product_id": 112, "trading_status": "live"},
            "UNKNOWN-999": {"type": "perp", "product_id": 999, "trading_status": "not_tradable"},
        }
    },
}


def _mk_sdk():
    """Build an SDK with _query mocked to return the symbols endpoint."""
    from dexes.nado.nado_sdk import NadoSDK
    sdk = NadoSDK.__new__(NadoSDK)
    sdk._query = AsyncMock(side_effect=lambda q: SYMBOLS_RESPONSE if q == "symbols" else {"status": "failure"})
    sdk._symbols_cache = None
    sdk._symbols_cache_time = 0
    sdk._symbols_ttl = 3600.0
    return sdk


@pytest.mark.asyncio
async def test_fetch_symbols_returns_live_mapping():
    from dexes.nado.nado_sdk import NadoSDK
    sdk = _mk_sdk()
    mapping = await sdk.fetch_symbols_map()
    assert mapping[2] == "BTC-PERP"
    assert mapping[110] == "MSFT-PERP"
    assert mapping[96] == "AAPL-PERP"
    assert mapping[112] == "AMZN-PERP"


@pytest.mark.asyncio
async def test_fetch_symbols_caches_within_ttl():
    sdk = _mk_sdk()
    await sdk.fetch_symbols_map()
    await sdk.fetch_symbols_map()
    await sdk.fetch_symbols_map()
    assert sdk._query.call_count == 1


@pytest.mark.asyncio
async def test_fetch_symbols_force_refresh():
    sdk = _mk_sdk()
    await sdk.fetch_symbols_map()
    await sdk.fetch_symbols_map(force_refresh=True)
    assert sdk._query.call_count == 2


@pytest.mark.asyncio
async def test_fetch_symbols_falls_back_on_api_failure():
    """If the API call fails, return whatever we had cached or the legacy hardcoded dict."""
    from dexes.nado.nado_sdk import NadoSDK
    sdk = NadoSDK.__new__(NadoSDK)
    sdk._query = AsyncMock(return_value={"status": "failure", "error": "timeout"})
    sdk._symbols_cache = None
    sdk._symbols_cache_time = 0
    sdk._symbols_ttl = 3600.0
    mapping = await sdk.fetch_symbols_map()
    assert mapping[2] == "BTC-PERP"  # falls back to PRODUCT_SYMBOLS
