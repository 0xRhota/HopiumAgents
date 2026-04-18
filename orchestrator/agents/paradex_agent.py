"""
Paradex Exchange Agent

Handles all interactions with Paradex DEX.
"""

import os
import sys
from typing import Dict, List, Optional
from dotenv import load_dotenv

from .base_agent import BaseAgent
from .. import logger as log

# Load environment variables
load_dotenv()


class ParadexAgent(BaseAgent):
    """
    Paradex DEX Agent.

    Features:
    - 0% maker fees
    - 0.02% taker fees
    - BTC-USD-PERP, ETH-USD-PERP
    - Python 3.11 required for paradex_py
    """

    def __init__(self):
        super().__init__("paradex")
        self.client = None
        self.initialized = False
        self.available_markets = {}  # symbol -> market_id mapping, fetched dynamically

    async def initialize(self) -> bool:
        """Initialize the Paradex client."""
        try:
            # Import paradex SDK - requires Python 3.11
            import sys
            if sys.version_info < (3, 11):
                log.log_warning("ParadexAgent requires Python 3.11+ for paradex_py")
                return False

            from paradex_py import Paradex

            private_key = os.getenv("PARADEX_PRIVATE_SUBKEY")
            if not private_key:
                log.log_warning("PARADEX_PRIVATE_SUBKEY not set - ParadexAgent disabled")
                return False

            # Create client
            self.client = await Paradex.create(
                network="mainnet",
                l1_private_key=private_key
            )

            # Fetch all available perpetual markets dynamically
            await self._fetch_available_markets()

            self.initialized = True
            log.log_info(f"ParadexAgent initialized with {len(self.available_markets)} markets")
            return True

        except ImportError as e:
            log.log_warning(f"ParadexAgent: paradex_py not available - {e}")
            return False
        except Exception as e:
            log.log_error(e, "ParadexAgent.initialize")
            return False

    async def _fetch_available_markets(self) -> None:
        """Fetch all available perpetual markets from the exchange."""
        try:
            markets = await self.client.markets.list()
            self.available_markets = {}

            for market in markets.results:
                symbol = market.symbol if hasattr(market, 'symbol') else str(market)
                # Only include perpetual markets (PERP suffix)
                if 'PERP' in symbol:
                    # Extract base symbol (e.g., "BTC-USD-PERP" -> "BTC")
                    base = symbol.replace('-USD-PERP', '').replace('-PERP', '')
                    self.available_markets[base] = symbol

            log.log_info(f"Paradex available markets: {list(self.available_markets.keys())}")

        except Exception as e:
            log.log_error(e, "ParadexAgent._fetch_available_markets")
            # Fallback to common markets
            self.available_markets = {
                "BTC": "BTC-USD-PERP",
                "ETH": "ETH-USD-PERP",
            }

    def get_available_symbols(self) -> List[str]:
        """Get list of available trading symbols."""
        return list(self.available_markets.keys())

    def _get_market_id(self, symbol: str) -> str:
        """Convert base symbol to market ID."""
        return self.available_markets.get(symbol, f"{symbol}-USD-PERP")

    async def get_balance(self) -> Optional[float]:
        """Get account balance in USD."""
        if not self.initialized:
            return None

        try:
            account = await self.client.account.get()
            if account:
                return float(account.account_value)
        except Exception as e:
            log.log_error(e, "ParadexAgent.get_balance")

        return None

    async def get_positions(self) -> List[Dict]:
        """Get all open positions."""
        if not self.initialized:
            return []

        try:
            positions_response = await self.client.account.positions()
            positions = []

            for pos in positions_response.results:
                if float(pos.size) != 0:
                    size = float(pos.size)
                    entry_price = float(pos.avg_entry_price)
                    current_price = float(pos.mark_price) if hasattr(pos, 'mark_price') else entry_price
                    unrealized_pnl = float(pos.unrealized_pnl) if hasattr(pos, 'unrealized_pnl') else 0

                    positions.append({
                        "symbol": pos.market,
                        "side": "LONG" if size > 0 else "SHORT",
                        "size": abs(size),
                        "size_usd": abs(size) * current_price,
                        "entry_price": entry_price,
                        "current_price": current_price,
                        "unrealized_pnl": unrealized_pnl,
                        "unrealized_pnl_pct": (unrealized_pnl / (abs(size) * entry_price)) * 100 if size != 0 and entry_price != 0 else 0
                    })

            return positions

        except Exception as e:
            log.log_error(e, "ParadexAgent.get_positions")
            return []

    async def get_pnl(self, hours: int = 24) -> Dict:
        """Get P&L data."""
        if not self.initialized:
            return {"realized": 0, "unrealized": 0, "fees": 0}

        try:
            account = await self.client.account.get()
            positions = await self.get_positions()

            unrealized = sum(p.get("unrealized_pnl", 0) for p in positions)

            return {
                "realized": 0,  # Paradex doesn't have easy realized P&L API
                "unrealized": unrealized,
                "fees": 0
            }
        except Exception as e:
            log.log_error(e, "ParadexAgent.get_pnl")
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
        """Place an order on Paradex."""
        if not self.initialized:
            return {"success": False, "error": "Not initialized"}

        try:
            from paradex_py.common.order import Order

            # Map symbol to market ID
            market = self._get_market_id(symbol)

            # Get current price if not provided
            if price is None:
                price = await self.get_current_price(symbol)
                if price is None:
                    return {"success": False, "error": "Could not get price"}

            # Calculate size in asset units
            size = size_usd / price

            # Build order
            order_side = "BUY" if side.upper() == "BUY" else "SELL"

            if order_type == "MARKET":
                # Use aggressive limit for market-like behavior
                limit_price = price * 1.005 if order_side == "BUY" else price * 0.995
            else:
                limit_price = price

            order = Order(
                market=market,
                order_type="LIMIT",
                order_side=order_side,
                size=size,
                limit_price=limit_price,
                instruction="POST_ONLY" if order_type == "LIMIT" else "GTC",
                reduce_only=reduce_only
            )

            result = await self.client.orders.create(order)

            return {
                "success": True,
                "order_id": result.id if hasattr(result, 'id') else str(result),
                "filled_size": size,
                "filled_price": limit_price,
                "error": None
            }

        except Exception as e:
            log.log_error(e, f"ParadexAgent.place_order({symbol}, {side})")
            return {"success": False, "error": str(e)}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order."""
        if not self.initialized:
            return False

        try:
            await self.client.orders.cancel(order_id)
            return True
        except Exception as e:
            log.log_error(e, f"ParadexAgent.cancel_order({order_id})")
            return False

    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price for a symbol."""
        if not self.initialized:
            return None

        try:
            market = self._get_market_id(symbol)
            markets = await self.client.markets.list()

            for m in markets.results:
                if m.symbol == market:
                    return float(m.mark_price) if hasattr(m, 'mark_price') else None

        except Exception as e:
            log.log_error(e, f"ParadexAgent.get_current_price({symbol})")

        return None
