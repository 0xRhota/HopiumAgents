"""Multi-asset portfolio with leverage-aware buying power.

Used by the backtest runner. Mirrors (but does not depend on) the live
account abstraction. Equity starts at starting_equity and moves with
every open/close: open_position(fee) subtracts fee from equity;
close_position(fee) adds realized PnL minus fee.

Fees are signed — positive = paid, negative = rebate. This matches the
Fill dataclass sign convention in core.reconciliation.base.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Literal


Side = Literal["LONG", "SHORT"]


@dataclass
class Portfolio:
    starting_equity: float
    leverage: float = 1.0
    equity: float = field(init=False)
    positions: Dict[str, dict] = field(default_factory=dict)

    def __post_init__(self):
        self.equity = self.starting_equity

    @property
    def buying_power(self) -> float:
        return self.equity * self.leverage

    def open_position(self, symbol: str, side: Side, size: float, price: float,
                      fee: float, ts: datetime, is_maker: bool) -> None:
        if symbol in self.positions:
            raise ValueError(f"already in position on {symbol}")
        if side not in ("LONG", "SHORT"):
            raise ValueError(f"invalid side: {side!r}")
        self.positions[symbol] = {
            "side": side,
            "size": size,
            "entry_price": price,
            "entry_ts": ts,
            "entry_fee": fee,
            "is_maker_entry": is_maker,
        }
        self.equity -= fee

    def close_position(self, symbol: str, price: float, fee: float,
                       ts: datetime, is_maker: bool) -> float:
        pos = self.positions.pop(symbol)
        if pos["side"] == "LONG":
            realized = (price - pos["entry_price"]) * pos["size"]
        else:
            realized = (pos["entry_price"] - price) * pos["size"]
        self.equity += realized - fee
        return realized
