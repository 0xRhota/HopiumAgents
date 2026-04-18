"""
Extended Exchange Agent

Handles all interactions with Extended DEX.
"""

import os
import sys
from typing import Dict, List, Optional
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from .base_agent import BaseAgent
from .. import logger as log

# Load environment variables
load_dotenv()


class ExtendedAgent(BaseAgent):
    """
    Extended DEX Agent.

    Features:
    - 0% maker fees
    - 0.025% taker fees
    - BTC-USD, ETH-USD, SOL-USD, XCU-USD (unique!)
    """

    def __init__(self):
        super().__init__("extended")
        self.sdk = None
        self.initialized = False
        self.available_markets = {}  # symbol -> market_id mapping, fetched dynamically

    async def initialize(self) -> bool:
        """Initialize the Extended SDK."""
        try:
            from dexes.extended.extended_sdk import ExtendedSDK

            private_key = os.getenv("EXTENDED_STARK_PRIVATE_KEY")
            account_address = os.getenv("EXTENDED_VAULT")

            if not all([private_key, account_address]):
                log.log_warning("EXTENDED credentials not set - ExtendedAgent disabled")
                return False

            self.sdk = ExtendedSDK(private_key, account_address)

            # Fetch all available markets dynamically
            await self._fetch_available_markets()

            self.initialized = True
            log.log_info(f"ExtendedAgent initialized with {len(self.available_markets)} markets")
            return True

        except Exception as e:
            log.log_error(e, "ExtendedAgent.initialize")
            return False

    async def _fetch_available_markets(self) -> None:
        """Fetch all available markets from the exchange."""
        try:
            markets = await self.sdk.get_markets()
            self.available_markets = {}

            for market in markets:
                symbol = market.get('symbol', '')
                # Include USD perpetual markets
                if '-USD' in symbol:
                    # Extract base symbol (e.g., "BTC-USD" -> "BTC")
                    base = symbol.replace('-USD', '')
                    self.available_markets[base] = symbol

            log.log_info(f"Extended available markets: {list(self.available_markets.keys())}")

        except Exception as e:
            log.log_error(e, "ExtendedAgent._fetch_available_markets")
            # Fallback to common markets
            self.available_markets = {
                "BTC": "BTC-USD",
                "ETH": "ETH-USD",
                "SOL": "SOL-USD",
            }

    def get_available_symbols(self) -> List[str]:
        """Get list of available trading symbols."""
        return list(self.available_markets.keys())

    def _get_market_id(self, symbol: str) -> str:
        """Convert base symbol to market ID."""
        return self.available_markets.get(symbol, f"{symbol}-USD")

    async def get_balance(self) -> Optional[float]:
        """Get account balance in USD."""
        if not self.initialized:
            return None

        try:
            balance_data = await self.sdk.get_balance()
            if balance_data:
                return float(balance_data.get('total_equity', 0))
        except Exception as e:
            log.log_error(e, "ExtendedAgent.get_balance")

        return None

    async def get_positions(self) -> List[Dict]:
        """Get all open positions."""
        if not self.initialized:
            return []

        try:
            raw_positions = await self.sdk.get_positions()
            positions = []

            if raw_positions and raw_positions.get('positions'):
                for pos in raw_positions['positions']:
                    size = float(pos.get('size', 0))
                    if size != 0:
                        entry_price = float(pos.get('entry_price', 0))
                        current_price = float(pos.get('mark_price', entry_price))
                        unrealized_pnl = float(pos.get('unrealized_pnl', 0))

                        positions.append({
                            "symbol": pos.get('market', 'UNKNOWN'),
                            "side": "LONG" if size > 0 else "SHORT",
                            "size": abs(size),
                            "size_usd": abs(size) * current_price,
                            "entry_price": entry_price,
                            "current_price": current_price,
                            "unrealized_pnl": unrealized_pnl,
                            "unrealized_pnl_pct": (unrealized_pnl / (abs(size) * entry_price)) * 100 if entry_price != 0 and size != 0 else 0
                        })

            return positions

        except Exception as e:
            log.log_error(e, "ExtendedAgent.get_positions")
            return []

    async def get_pnl(self, hours: int = 24) -> Dict:
        """Get P&L data."""
        if not self.initialized:
            return {"realized": 0, "unrealized": 0, "fees": 0}

        try:
            positions = await self.get_positions()
            unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)

            return {
                "realized": 0,
                "unrealized": unrealized,
                "fees": 0
            }
        except Exception as e:
            log.log_error(e, "ExtendedAgent.get_pnl")
            return {"realized": 0, "unrealized": 0, "fees": 0}

    async def place_order(
        self,
        symbol: str,
        side: str,
        size_usd: float,
        order_type: str = "LIMIT",
        price: Optional[float] = None,
        reduce_only: bool = False
    ) -> Dict:
        """Place an order on Extended."""
        if not self.initialized:
            return {"success": False, "error": "Not initialized"}

        try:
            # Map symbol to market ID
            market = self._get_market_id(symbol)

            # Get current price if not provided
            if price is None:
                price = await self.get_current_price(symbol)
                if price is None:
                    return {"success": False, "error": "Could not get price"}

            # Calculate size in asset units
            size = size_usd / price

            is_buy = side.upper() == "BUY"

            if order_type == "MARKET":
                result = await self.sdk.create_market_order(
                    symbol=market,
                    is_buy=is_buy,
                    size=size,
                    reduce_only=reduce_only
                )
            else:
                # Use limit order
                result = await self.sdk.create_limit_order(
                    symbol=market,
                    is_buy=is_buy,
                    size=size,
                    price=price,
                    reduce_only=reduce_only,
                    post_only=True
                )

            if result and result.get('success'):
                return {
                    "success": True,
                    "order_id": result.get('order_id', 'N/A'),
                    "filled_size": size,
                    "filled_price": price,
                    "error": None
                }
            else:
                return {
                    "success": False,
                    "error": result.get('error', 'Unknown error') if result else 'No result'
                }

        except Exception as e:
            log.log_error(e, f"ExtendedAgent.place_order({symbol}, {side})")
            return {"success": False, "error": str(e)}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order."""
        if not self.initialized:
            return False

        try:
            result = await self.sdk.cancel_order(order_id)
            return result.get('success', False) if result else False
        except Exception as e:
            log.log_error(e, f"ExtendedAgent.cancel_order({order_id})")
            return False

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price for a symbol."""
        if not self.initialized:
            return None

        try:
            market = self._get_market_id(symbol)
            markets = await self.sdk.get_markets()
            for m in markets:
                if m.get('symbol') == market:
                    return float(m.get('mark_price', 0))
        except Exception as e:
            log.log_error(e, f"ExtendedAgent.get_current_price({symbol})")

        return None
