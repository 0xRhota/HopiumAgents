"""
Base Agent Class

Abstract base class for all exchange agents.
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from datetime import datetime, timezone


class BaseAgent(ABC):
    """
    Base class for exchange agents.

    Each agent handles:
    - Balance queries
    - Position queries
    - Order execution
    - P&L tracking
    """

    def __init__(self, exchange_name: str):
        self.exchange_name = exchange_name
        self.last_balance: Optional[float] = None
        self.last_positions: List[Dict] = []
        self.last_update: Optional[datetime] = None

    @abstractmethod
    async def initialize(self) -> bool:
        """
        Initialize the agent (load credentials, create SDK instance).
        Returns True if successful.
        """
        pass

    @abstractmethod
    async def get_balance(self) -> Optional[float]:
        """
        Get current account balance in USD.
        MUST query exchange API directly.
        """
        pass

    @abstractmethod
    async def get_positions(self) -> List[Dict]:
        """
        Get all open positions.
        MUST query exchange API directly.

        Returns list of:
        {
            "symbol": str,
            "side": "LONG" | "SHORT",
            "size": float,  # In asset units
            "size_usd": float,  # In USD
            "entry_price": float,
            "current_price": float,
            "unrealized_pnl": float,
            "unrealized_pnl_pct": float
        }
        """
        pass

    @abstractmethod
    async def get_pnl(self, hours: int = 24) -> Dict:
        """
        Get realized P&L from exchange API.

        Returns:
        {
            "realized": float,
            "unrealized": float,
            "fees": float
        }
        """
        pass

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,  # "BUY" or "SELL"
        size_usd: float,
        order_type: str = "LIMIT",  # "LIMIT" or "MARKET"
        price: Optional[float] = None,
        reduce_only: bool = False
    ) -> Dict:
        """
        Place an order.

        Returns:
        {
            "success": bool,
            "order_id": str | None,
            "filled_size": float,
            "filled_price": float,
            "error": str | None
        }
        """
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by ID."""
        pass

    @abstractmethod
    async def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current market price for a symbol."""
        pass

    async def refresh(self) -> Dict:
        """
        Refresh all state from exchange API.

        Returns:
        {
            "balance": float,
            "positions": List[Dict],
            "success": bool,
            "error": str | None
        }
        """
        try:
            balance = await self.get_balance()
            positions = await self.get_positions()

            self.last_balance = balance
            self.last_positions = positions
            self.last_update = datetime.now(timezone.utc)

            return {
                "balance": balance,
                "positions": positions,
                "success": True,
                "error": None
            }
        except Exception as e:
            return {
                "balance": self.last_balance,
                "positions": self.last_positions,
                "success": False,
                "error": str(e)
            }

    def get_position(self, symbol: str) -> Optional[Dict]:
        """Get a specific position from cached data."""
        for pos in self.last_positions:
            if symbol.upper() in pos.get("symbol", "").upper():
                return pos
        return None

    def has_position(self, symbol: str) -> bool:
        """Check if we have a position in this symbol."""
        return self.get_position(symbol) is not None

    def get_position_side(self, symbol: str) -> Optional[str]:
        """Get the side of our position (LONG/SHORT) or None."""
        pos = self.get_position(symbol)
        return pos.get("side") if pos else None

    async def close_position(self, symbol: str) -> Dict:
        """
        Close an existing position.

        Returns same format as place_order.
        """
        pos = self.get_position(symbol)
        if not pos:
            return {"success": False, "error": f"No position in {symbol}"}

        # Close by placing opposite order
        close_side = "SELL" if pos["side"] == "LONG" else "BUY"
        return await self.place_order(
            symbol=symbol,
            side=close_side,
            size_usd=pos["size_usd"],
            order_type="MARKET",
            reduce_only=True
        )

    def to_dict(self) -> Dict:
        """Export current state as dictionary."""
        return {
            "exchange": self.exchange_name,
            "balance": self.last_balance,
            "positions": self.last_positions,
            "last_update": self.last_update.isoformat() if self.last_update else None
        }
