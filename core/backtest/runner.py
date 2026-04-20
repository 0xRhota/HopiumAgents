"""Event-driven backtest loop.

Strategy interface: an object with a method

    on_bar(ts, bar, portfolio, history=None) -> List[dict]

where each returned dict is either:
    {"action": "OPEN",  "symbol": str, "side": "LONG"|"SHORT",
     "size": float (USD notional), "limit_price": float, "post_only": bool}
    {"action": "CLOSE", "symbol": str,
     "limit_price": float, "post_only": bool}

Output: List[Fill] matching core.reconciliation.base.Fill schema.
Same schema as live ledger → sim results are directly comparable via
scripts/validate_strategy.py.
"""
from __future__ import annotations

from typing import List

import pandas as pd

from core.backtest.exchange_sim import ExchangeSpec, simulate_order
from core.backtest.portfolio import Portfolio
from core.reconciliation.base import Fill


def run_backtest(strategy, bars: pd.DataFrame, exchange: ExchangeSpec,
                 starting_equity: float, leverage: float = 1.0,
                 half_spread_bps: float = 5.0) -> List[Fill]:
    portfolio = Portfolio(starting_equity=starting_equity, leverage=leverage)
    fills: List[Fill] = []
    fill_counter = 0

    for ts, row in bars.iterrows():
        actions = strategy.on_bar(ts, row, portfolio, history=bars.loc[:ts])
        for act in actions:
            symbol = act["symbol"]
            mid = float(row["close"])
            half_spread = mid * (half_spread_bps / 10_000.0)
            limit_price = float(act["limit_price"])
            post_only = bool(act.get("post_only", False))

            if act["action"] == "OPEN":
                side_order = "BUY" if act["side"] == "LONG" else "SELL"
                size = float(act["size"]) / mid  # notional $ → base units
                sim = simulate_order(exchange, side=side_order, size=size,
                                     price=limit_price, mid=mid,
                                     half_spread=half_spread, post_only=post_only)
                if sim is None:
                    continue
                portfolio.open_position(symbol=symbol, side=act["side"],
                                        size=size, price=sim["fill_price"],
                                        fee=sim["fee"], ts=ts,
                                        is_maker=sim["is_maker"])
                fill_counter += 1
                fills.append(Fill(
                    exchange=exchange.name, symbol=symbol,
                    fill_id=f"sim-{fill_counter}",
                    order_id=f"sim-{fill_counter}",
                    ts=ts, side=side_order, size=size,
                    price=sim["fill_price"], fee=sim["fee"],
                    is_maker=sim["is_maker"],
                    realized_pnl_usd=None, opens_or_closes="OPEN",
                ))

            elif act["action"] == "CLOSE":
                if symbol not in portfolio.positions:
                    continue
                pos = portfolio.positions[symbol]
                side_order = "SELL" if pos["side"] == "LONG" else "BUY"
                sim = simulate_order(exchange, side=side_order, size=pos["size"],
                                     price=limit_price, mid=mid,
                                     half_spread=half_spread, post_only=post_only)
                if sim is None:
                    continue
                realized = portfolio.close_position(
                    symbol=symbol, price=sim["fill_price"], fee=sim["fee"],
                    ts=ts, is_maker=sim["is_maker"],
                )
                fill_counter += 1
                fills.append(Fill(
                    exchange=exchange.name, symbol=symbol,
                    fill_id=f"sim-{fill_counter}",
                    order_id=f"sim-{fill_counter}",
                    ts=ts, side=side_order, size=pos["size"],
                    price=sim["fill_price"], fee=sim["fee"],
                    is_maker=sim["is_maker"],
                    realized_pnl_usd=realized, opens_or_closes="CLOSE",
                ))

    return fills
