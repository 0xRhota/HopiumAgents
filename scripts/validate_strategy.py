"""Trust gate: compare backtest sim PnL to live ledger PnL for a window.

Run BEFORE trusting any backtest result for strategy decisions.

Usage:
    python3.11 scripts/validate_strategy.py --exchange hibachi --symbol BTC/USDT-P --days 7
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from core.backtest.compare import compare_pnl
from core.backtest.exchange_sim import NADO, HIBACHI, PARADEX
from core.backtest.momentum_strategy import BacktestMomentumStrategy
from core.backtest.runner import run_backtest
from core.reconciliation import build_reconciler
from scripts.run_backtest import fetch_binance_klines


_EX_MAP = {"nado": NADO, "hibachi": HIBACHI, "paradex": PARADEX}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", required=True, choices=list(_EX_MAP))
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--tolerance-usd", type=float, default=1.0)
    ap.add_argument("--tolerance-pct", type=float, default=0.05)
    ap.add_argument("--preset", choices=["fast", "slow"], default="fast",
                    help="Strategy preset to simulate (must match what live runs)")
    args = ap.parse_args()

    bars = fetch_binance_klines(args.symbol, args.days)
    if len(bars) < 50:
        print(f"Not enough bars ({len(bars)})")
        sys.exit(2)

    strategy = BacktestMomentumStrategy(
        symbol=args.symbol, preset=args.preset,
        exchange=args.exchange,  # enables self-learning + cooldown from live history
    )
    sim_fills = run_backtest(
        strategy=strategy, bars=bars,
        exchange=_EX_MAP[args.exchange],
        starting_equity=100.0, leverage=10.0,
    )

    rec = build_reconciler(args.exchange)
    snap = await rec.snapshot()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    live_fills = [f for f in snap.new_fills
                  if f.symbol == args.symbol and f.ts >= cutoff]

    r = compare_pnl(sim_fills, live_fills,
                    tolerance_usd=args.tolerance_usd,
                    tolerance_pct=args.tolerance_pct)

    print(f"\n=== {args.symbol} on {args.exchange} — last {args.days}d ===")
    print(f"sim:  realized=${r.sim_realized:+.2f}  fees=${r.sim_fees:+.2f}"
          f"  NET=${r.sim_net:+.2f}  ({len(sim_fills)} fills)")
    print(f"live: realized=${r.live_realized:+.2f}  fees=${r.live_fees:+.2f}"
          f"  NET=${r.live_net:+.2f}  ({len(live_fills)} fills)")
    print(f"divergence: ${r.divergence_usd:.2f} ({r.divergence_pct*100:.1f}%)")
    print(f"PASSED: {r.passed}")
    print(f"notes: {r.notes}")
    sys.exit(0 if r.passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
