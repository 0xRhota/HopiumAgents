"""
Position Manager

Manages all open positions across exchanges:
- Entry/exit execution
- Trailing stops
- Time stops
- P&L tracking
"""

from typing import Dict, List, Optional
from datetime import datetime, timezone
from dataclasses import dataclass, field
import json
from pathlib import Path

from . import config
from . import logger as log


@dataclass
class Position:
    """Represents an open position."""
    id: str
    exchange: str
    symbol: str
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    size: float
    size_usd: float
    entry_time: datetime
    trade_type: str  # "SWING_HIGH", "SWING_STANDARD", "SCALP"
    stop_loss_pct: float
    take_profit_pct: float
    time_stop_hours: float
    trailing_stop_pct: Optional[float] = None
    peak_pnl_pct: float = 0.0
    current_stop_pct: Optional[float] = None
    closed: bool = False
    close_reason: Optional[str] = None
    close_price: Optional[float] = None
    close_time: Optional[datetime] = None
    realized_pnl: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "size": self.size,
            "size_usd": self.size_usd,
            "entry_time": self.entry_time.isoformat(),
            "trade_type": self.trade_type,
            "stop_loss_pct": self.stop_loss_pct,
            "take_profit_pct": self.take_profit_pct,
            "time_stop_hours": self.time_stop_hours,
            "trailing_stop_pct": self.trailing_stop_pct,
            "peak_pnl_pct": self.peak_pnl_pct,
            "current_stop_pct": self.current_stop_pct,
            "closed": self.closed,
            "close_reason": self.close_reason,
            "close_price": self.close_price,
            "close_time": self.close_time.isoformat() if self.close_time else None,
            "realized_pnl": self.realized_pnl
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "Position":
        return cls(
            id=data["id"],
            exchange=data["exchange"],
            symbol=data["symbol"],
            direction=data["direction"],
            entry_price=data["entry_price"],
            size=data["size"],
            size_usd=data["size_usd"],
            entry_time=datetime.fromisoformat(data["entry_time"]),
            trade_type=data["trade_type"],
            stop_loss_pct=data["stop_loss_pct"],
            take_profit_pct=data["take_profit_pct"],
            time_stop_hours=data["time_stop_hours"],
            trailing_stop_pct=data.get("trailing_stop_pct"),
            peak_pnl_pct=data.get("peak_pnl_pct", 0.0),
            current_stop_pct=data.get("current_stop_pct"),
            closed=data.get("closed", False),
            close_reason=data.get("close_reason"),
            close_price=data.get("close_price"),
            close_time=datetime.fromisoformat(data["close_time"]) if data.get("close_time") else None,
            realized_pnl=data.get("realized_pnl", 0.0)
        )


class PositionManager:
    """
    Manages all positions across exchanges.

    Features:
    - Track positions with entry time, price, etc.
    - Apply exit rules (TP, SL, time stop, trailing)
    - Persist state to disk
    """

    STATE_FILE = Path(config.LOG_DIR) / "swing_position_state.json"

    def __init__(self):
        self.positions: Dict[str, Position] = {}  # id -> Position
        self._load_state()

    def _load_state(self) -> None:
        """Load position state from disk."""
        if self.STATE_FILE.exists():
            try:
                with open(self.STATE_FILE, "r") as f:
                    data = json.load(f)
                    for pos_data in data.get("positions", []):
                        pos = Position.from_dict(pos_data)
                        if not pos.closed:
                            self.positions[pos.id] = pos
                log.log_info(f"Loaded {len(self.positions)} open positions from state")
            except Exception as e:
                log.log_error(e, "PositionManager._load_state")

    def _save_state(self) -> None:
        """Save position state to disk."""
        try:
            self.STATE_FILE.parent.mkdir(exist_ok=True)
            data = {
                "positions": [p.to_dict() for p in self.positions.values()],
                "last_updated": datetime.now(timezone.utc).isoformat()
            }
            with open(self.STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.log_error(e, "PositionManager._save_state")

    def create_position(
        self,
        exchange: str,
        symbol: str,
        direction: str,
        entry_price: float,
        size: float,
        size_usd: float,
        trade_type: str
    ) -> Position:
        """
        Create a new position record.

        Args:
            exchange: Exchange name
            symbol: Asset symbol
            direction: "LONG" or "SHORT"
            entry_price: Entry price
            size: Size in asset units
            size_usd: Size in USD
            trade_type: "SWING_HIGH", "SWING_STANDARD", or "SCALP"
        """
        # Generate unique ID
        pos_id = f"{exchange}_{symbol}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"

        # Get exit parameters based on trade type
        if trade_type == "SWING_HIGH":
            tp = config.TP_HIGH_CONVICTION
            sl = config.SL_HIGH_CONVICTION
            ts = config.TIME_STOP_HIGH_CONVICTION
        elif trade_type == "SCALP":
            tp = config.TP_SCALP
            sl = config.SL_SCALP
            ts = config.TIME_STOP_SCALP
        else:
            tp = config.TP_STANDARD
            sl = config.SL_STANDARD
            ts = config.TIME_STOP_STANDARD

        position = Position(
            id=pos_id,
            exchange=exchange,
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            size=size,
            size_usd=size_usd,
            entry_time=datetime.now(timezone.utc),
            trade_type=trade_type,
            stop_loss_pct=sl,
            take_profit_pct=tp,
            time_stop_hours=ts
        )

        self.positions[pos_id] = position
        self._save_state()

        log.log_info(f"Created position: {pos_id} - {direction} {symbol} @ ${entry_price:.2f}")
        return position

    def get_position(self, pos_id: str) -> Optional[Position]:
        """Get a position by ID."""
        return self.positions.get(pos_id)

    def get_positions_for_exchange(self, exchange: str) -> List[Position]:
        """Get all open positions for an exchange."""
        return [p for p in self.positions.values() if p.exchange == exchange and not p.closed]

    def get_position_for_symbol(self, exchange: str, symbol: str) -> Optional[Position]:
        """Get open position for a specific symbol on an exchange."""
        for p in self.positions.values():
            if p.exchange == exchange and p.symbol == symbol and not p.closed:
                return p
        return None

    def get_all_open_positions(self) -> List[Position]:
        """Get all open positions."""
        return [p for p in self.positions.values() if not p.closed]

    def calculate_pnl(self, position: Position, current_price: float) -> Dict:
        """
        Calculate P&L for a position.

        Returns:
            {
                "pnl_usd": float,
                "pnl_pct": float
            }
        """
        if position.direction == "LONG":
            pnl_pct = ((current_price - position.entry_price) / position.entry_price) * 100
        else:
            pnl_pct = ((position.entry_price - current_price) / position.entry_price) * 100

        pnl_usd = position.size_usd * (pnl_pct / 100)

        return {
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct
        }

    def check_exit_rules(self, position: Position, current_price: float) -> Optional[str]:
        """
        Check if any exit rules are triggered.

        Returns:
            Exit reason if triggered, None otherwise
        """
        pnl = self.calculate_pnl(position, current_price)
        pnl_pct = pnl["pnl_pct"]

        # Update peak P&L for trailing stop
        if pnl_pct > position.peak_pnl_pct:
            position.peak_pnl_pct = pnl_pct

            # Check trailing stop triggers
            if pnl_pct >= config.TRAILING_START_TRIGGER * 100:
                # Start trailing
                position.current_stop_pct = pnl_pct - (config.TRAILING_DISTANCE * 100)
                log.log_info(f"Trailing stop updated: {position.id} - stop at {position.current_stop_pct:.1f}%")
            elif pnl_pct >= config.TRAILING_BREAKEVEN_TRIGGER * 100:
                # Move to breakeven
                if position.current_stop_pct is None or position.current_stop_pct < 0:
                    position.current_stop_pct = 0.0
                    log.log_info(f"Breakeven stop activated: {position.id}")

        # Check take profit
        if pnl_pct >= position.take_profit_pct * 100:
            return f"TAKE_PROFIT ({pnl_pct:.1f}%)"

        # Check stop loss
        if pnl_pct <= -(position.stop_loss_pct * 100):
            return f"STOP_LOSS ({pnl_pct:.1f}%)"

        # Check trailing stop
        if position.current_stop_pct is not None and pnl_pct <= position.current_stop_pct:
            return f"TRAILING_STOP (peak: {position.peak_pnl_pct:.1f}%, exit: {pnl_pct:.1f}%)"

        # Check time stop
        hours_held = (datetime.now(timezone.utc) - position.entry_time).total_seconds() / 3600
        if hours_held >= position.time_stop_hours:
            return f"TIME_STOP ({hours_held:.1f}h)"

        return None

    def close_position(
        self,
        position: Position,
        close_price: float,
        reason: str
    ) -> Dict:
        """
        Close a position.

        Returns:
            {
                "realized_pnl": float,
                "hold_time_hours": float
            }
        """
        pnl = self.calculate_pnl(position, close_price)

        position.closed = True
        position.close_reason = reason
        position.close_price = close_price
        position.close_time = datetime.now(timezone.utc)
        position.realized_pnl = pnl["pnl_usd"]

        hold_time = (position.close_time - position.entry_time).total_seconds() / 3600

        self._save_state()

        log.log_info(f"Closed position: {position.id}")
        log.log_info(f"  Reason: {reason}")
        log.log_info(f"  P&L: ${pnl['pnl_usd']:+.2f} ({pnl['pnl_pct']:+.1f}%)")
        log.log_info(f"  Hold time: {hold_time:.1f}h")

        return {
            "realized_pnl": pnl["pnl_usd"],
            "hold_time_hours": hold_time
        }

    def get_total_exposure(self) -> Dict:
        """
        Get total exposure across all exchanges.

        Returns:
            {
                "total_usd": float,
                "by_exchange": {exchange: float},
                "by_direction": {"LONG": float, "SHORT": float}
            }
        """
        by_exchange: Dict[str, float] = {}
        by_direction = {"LONG": 0.0, "SHORT": 0.0}
        total = 0.0

        for pos in self.get_all_open_positions():
            total += pos.size_usd
            by_exchange[pos.exchange] = by_exchange.get(pos.exchange, 0) + pos.size_usd
            by_direction[pos.direction] += pos.size_usd

        return {
            "total_usd": total,
            "by_exchange": by_exchange,
            "by_direction": by_direction
        }

    def can_open_position(self, exchange: str) -> bool:
        """Check if we can open a new position on this exchange."""
        exchange_positions = len(self.get_positions_for_exchange(exchange))
        total_positions = len(self.get_all_open_positions())

        return (
            exchange_positions < config.MAX_POSITIONS_PER_EXCHANGE and
            total_positions < config.MAX_TOTAL_POSITIONS
        )

    def sync_with_exchange(self, exchange: str, exchange_positions: List[Dict]) -> None:
        """
        Sync local state with exchange positions.

        This detects:
        - Positions closed externally (liquidation, manual close)
        - Positions opened externally
        """
        # Get our tracked positions for this exchange
        our_positions = {p.symbol: p for p in self.get_positions_for_exchange(exchange)}

        # Get exchange positions as dict
        exchange_pos_map = {}
        for ep in exchange_positions:
            # Normalize symbol
            symbol = ep.get("symbol", "").replace("-PERP", "").replace("-USD", "").replace("/USDT-P", "")
            exchange_pos_map[symbol] = ep

        # Check for positions that disappeared (closed externally)
        for symbol, pos in our_positions.items():
            norm_symbol = symbol.replace("-PERP", "").replace("-USD", "").replace("/USDT-P", "")
            if norm_symbol not in exchange_pos_map:
                log.log_warning(f"Position {pos.id} closed externally")
                pos.closed = True
                pos.close_reason = "EXTERNAL_CLOSE"
                pos.close_time = datetime.now(timezone.utc)

        self._save_state()


# Global instance
_manager: Optional[PositionManager] = None


def get_manager() -> PositionManager:
    """Get or create the global position manager."""
    global _manager
    if _manager is None:
        _manager = PositionManager()
    return _manager
