"""
Nado Exchange Agent

Handles all interactions with Nado DEX.
"""

import os
import sys
from typing import Dict, List, Optional
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from .base_agent import BaseAgent
from .. import logger as log
from .. import config

# Load environment variables
load_dotenv()


class NadoAgent(BaseAgent):
    """
    Nado DEX Agent.

    Features:
    - 0% maker fees with POST_ONLY
    - 0.035% taker fees
    - ETH-PERP, BTC-PERP, SOL-PERP
    - CRITICAL: $100 minimum order size
    """

    def __init__(self):
        super().__init__("nado")
        self.sdk = None
        self.initialized = False
        self.available_markets = {}  # symbol -> market_id mapping, fetched dynamically

    async def initialize(self) -> bool:
        """Initialize the Nado SDK."""
        try:
            from dexes.nado.nado_sdk import NadoSDK

            wallet_address = os.getenv("NADO_WALLET_ADDRESS")
            private_key = os.getenv("NADO_LINKED_SIGNER_PRIVATE_KEY")
            subaccount = os.getenv("NADO_SUBACCOUNT_NAME", "default")

            if not all([wallet_address, private_key]):
                log.log_warning("NADO credentials not set - NadoAgent disabled")
                return False

            self.sdk = NadoSDK(wallet_address, private_key, subaccount)

            # Fetch all available perpetual markets dynamically
            await self._fetch_available_markets()

            self.initialized = True
            log.log_info(f"NadoAgent initialized with {len(self.available_markets)} markets")
            return True

        except Exception as e:
            log.log_error(e, "NadoAgent.initialize")
            return False

    async def _fetch_available_markets(self) -> None:
        """Fetch all available perpetual markets from the exchange."""
        try:
            products = await self.sdk.get_products()
            self.available_markets = {}

            for product in products:
                symbol = product.get('symbol', '')
                # Only include perpetual markets (PERP suffix)
                if 'PERP' in symbol:
                    # Extract base symbol (e.g., "BTC-PERP" -> "BTC")
                    base = symbol.replace('-PERP', '')
                    self.available_markets[base] = symbol

            log.log_info(f"Nado available markets: {list(self.available_markets.keys())}")

        except Exception as e:
            log.log_error(e, "NadoAgent._fetch_available_markets")
            # Fallback to common markets
            self.available_markets = {
                "BTC": "BTC-PERP",
                "ETH": "ETH-PERP",
                "SOL": "SOL-PERP",
            }

    def get_available_symbols(self) -> List[str]:
        """Get list of available trading symbols."""
        return list(self.available_markets.keys())

    def _get_market_id(self, symbol: str) -> str:
        """Convert base symbol to market ID."""
        return self.available_markets.get(symbol, f"{symbol}-PERP")

    async def get_balance(self) -> Optional[float]:
        """Get account balance in USD."""
        if not self.initialized:
            return None

        try:
            return await self.sdk.get_balance()
        except Exception as e:
            log.log_error(e, "NadoAgent.get_balance")
            return None

    async def get_positions(self) -> List[Dict]:
        """Get all open positions."""
        if not self.initialized:
            return []

        try:
            raw_positions = await self.sdk.get_positions()
            positions = []

            for pos in raw_positions:
                size = float(pos.get('size', 0))
                if size != 0:
                    entry_price = float(pos.get('entry_price', 0))
                    current_price = float(pos.get('mark_price', entry_price))
                    unrealized_pnl = float(pos.get('unrealized_pnl', 0))

                    positions.append({
                        "symbol": pos.get('symbol', 'UNKNOWN'),
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
            log.log_error(e, "NadoAgent.get_positions")
            return []

    async def get_pnl(self, hours: int = 24) -> Dict:
        """Get P&L data from Nado Archive API."""
        if not self.initialized:
            return {"realized": 0, "unrealized": 0, "fees": 0}

        try:
            pnl_data = await self.sdk.get_pnl(hours)
            return {
                "realized": pnl_data.get("realized_pnl", 0),
                "unrealized": pnl_data.get("unrealized_pnl", 0),
                "fees": pnl_data.get("fees", 0)
            }
        except Exception as e:
            log.log_error(e, "NadoAgent.get_pnl")
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
        """Place an order on Nado."""
        if not self.initialized:
            return {"success": False, "error": "Not initialized"}

        # Check minimum order size
        if size_usd < config.EXCHANGE_CONFIG["nado"]["min_order_usd"]:
            return {
                "success": False,
                "error": f"Order size ${size_usd:.2f} below Nado minimum ${config.EXCHANGE_CONFIG['nado']['min_order_usd']}"
            }

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
                    amount=size,
                    current_price=price
                )
            else:
                # Use POST_ONLY for maker fees
                result = await self.sdk.create_limit_order(
                    symbol=market,
                    is_buy=is_buy,
                    amount=size,
                    price=price,
                    order_type="POST_ONLY",
                    reduce_only=reduce_only
                )

            if result and result.get('status') == 'success':
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
            log.log_error(e, f"NadoAgent.place_order({symbol}, {side})")
            return {"success": False, "error": str(e)}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order."""
        if not self.initialized:
            return False

        try:
            result = await self.sdk.cancel_order(order_id)
            return result.get('status') == 'success' if result else False
        except Exception as e:
            log.log_error(e, f"NadoAgent.cancel_order({order_id})")
            return False

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price for a symbol."""
        if not self.initialized:
            return None

        try:
            market = self._get_market_id(symbol)
            products = await self.sdk.get_products()
            for p in products:
                if p.get('symbol') == market:
                    return float(p.get('oracle_price', 0))
        except Exception as e:
            log.log_error(e, f"NadoAgent.get_current_price({symbol})")

        return None

    def can_trade(self, size_usd: float) -> bool:
        """Check if we can trade this size (Nado has $100 minimum)."""
        return size_usd >= config.EXCHANGE_CONFIG["nado"]["min_order_usd"]
