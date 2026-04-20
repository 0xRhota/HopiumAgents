"""Run a backtest of the momentum strategy on historical Binance klines.

Usage:
    python3 scripts/run_backtest.py --symbol BTC-PERP --exchange nado --days 14
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import requests

from core.backtest.exchange_sim import NADO, HIBACHI, PARADEX
from core.backtest.momentum_strategy import BacktestMomentumStrategy
from core.backtest.runner import run_backtest


_EX_MAP = {"nado": NADO, "hibachi": HIBACHI, "paradex": PARADEX}


def _to_binance_symbol(symbol: str) -> str:
    """Best-effort map from exchange-native symbol to Binance perp symbol."""
    s = symbol.upper().replace("-PERP", "").replace("/USDT-P", "").replace("-USD-PERP", "")
    if s.startswith("K") and len(s) > 1:
        s = "1000" + s[1:]  # kBONK → 1000BONK etc.
    return s + "USDT"


def fetch_binance_klines(symbol: str, days: int, interval: str = "15m") -> pd.DataFrame:
    binance_sym = _to_binance_symbol(symbol)
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = end - days * 24 * 60 * 60 * 1000

    frames = []
    cursor = start
    while cursor < end:
        resp = requests.get("https://api.binance.com/api/v3/klines", params={
            "symbol": binance_sym, "interval": interval,
            "startTime": cursor, "endTime": end, "limit": 1000,
        }, timeout=15)
        data = resp.json()
        if not isinstance(data, list) or not data:
            break
        frames.append(data)
        last_close = data[-1][6]
        next_cursor = last_close + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(data) < 1000:
            break

    rows = [row for batch in frames for row in batch]
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "tb_base", "tb_quote", "ignore",
    ])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["open", "high", "low", "close", "volume"]].drop_duplicates()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True, help="Exchange-native symbol (e.g. BTC-PERP, BTC/USDT-P)")
    ap.add_argument("--exchange", required=True, choices=list(_EX_MAP))
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--score-min", type=float, default=2.5)
    ap.add_argument("--tp-bps", type=float, default=80.0)
    ap.add_argument("--sl-bps", type=float, default=40.0)
    ap.add_argument("--starting-equity", type=float, default=100.0)
    ap.add_argument("--leverage", type=float, default=10.0)
    args = ap.parse_args()

    bars = fetch_binance_klines(args.symbol, args.days)
    if len(bars) < 50:
        print(f"Not enough bars ({len(bars)}) — need ≥50 for MomentumEngine signals")
        sys.exit(1)

    strategy = BacktestMomentumStrategy(
        symbol=args.symbol, score_min=args.score_min,
        tp_bps=args.tp_bps, sl_bps=args.sl_bps,
    )
    fills = run_backtest(
        strategy=strategy, bars=bars, exchange=_EX_MAP[args.exchange],
        starting_equity=args.starting_equity, leverage=args.leverage,
    )

    realized = sum(f.realized_pnl_usd or 0 for f in fills)
    fees = sum(f.fee for f in fills)
    net = realized - fees
    closes = [f for f in fills if f.opens_or_closes == "CLOSE"]
    wins = sum(1 for f in closes if (f.realized_pnl_usd or 0) > 0)
    wr = wins / len(closes) * 100 if closes else 0
    makers = sum(1 for f in fills if f.is_maker)
    maker_pct = makers / max(1, len(fills)) * 100

    print(f"\n=== Backtest: {args.symbol} on {args.exchange} ({args.days}d, {len(bars)} bars) ===")
    print(f"Strategy:  score>={args.score_min}  TP={args.tp_bps}bps  SL={args.sl_bps}bps  lev={args.leverage}x")
    print(f"Fills:     {len(fills)}  ({maker_pct:.0f}% maker)")
    print(f"Trades:    {len(closes)}")
    print(f"Win rate:  {wr:.0f}%")
    print(f"Realized:  ${realized:+.2f}")
    print(f"Fees:      ${fees:+.2f}")
    print(f"NET PnL:   ${net:+.2f}")


if __name__ == "__main__":
    main()
