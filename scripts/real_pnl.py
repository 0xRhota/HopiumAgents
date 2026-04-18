"""Real PnL reporter — queries exchange APIs via Reconcilers.

NEVER reads JSONL pnl field (which is gross-of-fees fiction).
Always queries exchange for fills + fees.

Usage:
    python3 scripts/real_pnl.py              # 24h window all exchanges
    python3 scripts/real_pnl.py --hours 168  # 7-day window
    python3 scripts/real_pnl.py --exchange paradex --hours 1
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from core.reconciliation import build_reconciler


async def _fetch_one(exchange: str, hours: int):
    r = build_reconciler(exchange)
    snap, window = await asyncio.gather(r.snapshot(), r.get_pnl_window(hours=hours))
    return exchange, snap, window


async def report(exchanges, hours: int):
    print(f"\n═══ Real PnL — last {hours}h (exchange truth, fees included) ═══\n")
    print(f"{'Exchange':<10} {'Equity':>10} {'Realized':>10} {'Fees':>10} {'Funding':>10} {'NET PnL':>10} {'Trades':>7}")
    print("─" * 78)

    results = await asyncio.gather(
        *(_fetch_one(ex, hours) for ex in exchanges),
        return_exceptions=True,
    )

    total_net = 0.0
    total_equity = 0.0
    for ex, result in zip(exchanges, results):
        if isinstance(result, Exception):
            print(f"{ex:<10} ERROR: {result}")
            continue
        _, snap, w = result
        total_net += w.net_pnl
        total_equity += snap.equity
        print(
            f"{ex:<10} ${snap.equity:>9.2f} ${w.realized_pnl:>+9.2f} ${w.fees_paid:>+9.2f} "
            f"${w.funding_paid:>+9.2f} ${w.net_pnl:>+9.2f} {w.trade_count:>7}"
        )
    print("─" * 78)
    print(f"{'TOTAL':<10} ${total_equity:>9.2f} {'':>10} {'':>10} {'':>10} ${total_net:>+9.2f}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", choices=["paradex", "nado", "hibachi"], help="One exchange; default=all")
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()
    exchanges = [args.exchange] if args.exchange else ["paradex", "nado", "hibachi"]
    asyncio.run(report(exchanges, args.hours))


if __name__ == "__main__":
    main()
