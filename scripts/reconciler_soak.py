"""Read-only reconciler soak runner.

Runs any exchange's Reconciler in parallel with the live bot, logging
snapshots to logs/reconciliation/{exchange}_soak.jsonl for comparison
against exchange dashboard reality.

Usage:
    python3.11 scripts/reconciler_soak.py --exchange paradex --interval 300

Never places orders. Safe to run alongside live bots.
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from core.reconciliation import build_reconciler
from core.reconciliation.base import ExchangeSnapshot
from core.reconciliation.ledger import DuplicateFillError, Ledger


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("reconciler_soak")


def _snapshot_to_jsonl(path: Path, snap: ExchangeSnapshot):
    path.parent.mkdir(parents=True, exist_ok=True)
    d = {
        "ts": snap.ts.isoformat(),
        "exchange": snap.exchange,
        "equity": snap.equity,
        "positions": [
            {
                "symbol": p.symbol, "side": p.side, "size": p.size,
                "entry_price": p.entry_price, "unrealized_pnl": p.unrealized_pnl,
                "funding_accrued": p.funding_accrued,
            }
            for p in snap.positions
        ],
        "total_notional": snap.total_notional,
        "total_unrealized": snap.total_unrealized,
        "new_fills_count": len(snap.new_fills),
        "funding_paid_since": snap.funding_paid_since,
    }
    with path.open("a") as f:
        f.write(json.dumps(d) + "\n")


async def run(exchange: str, interval: int):
    rec = build_reconciler(exchange)
    snap_log = Path(f"logs/reconciliation/{exchange}_soak.jsonl")
    ledger = Ledger(Path(f"logs/ledger/{exchange}_ledger.jsonl"))

    session_start = datetime.now(timezone.utc)
    session_start_equity = None
    last_snap_ts = None

    logger.info(f"[{exchange}] reconciler soak starting (interval={interval}s)")
    logger.info(f"[{exchange}] snapshots → {snap_log}")
    logger.info(f"[{exchange}] ledger    → {ledger.path}")

    while True:
        try:
            snap = await rec.snapshot(since=last_snap_ts)
            _snapshot_to_jsonl(snap_log, snap)

            if session_start_equity is None:
                session_start_equity = snap.equity

            # Append new fills to ledger (dedup via DuplicateFillError)
            new_appended = 0
            for fill in snap.new_fills:
                try:
                    ledger.append(fill)
                    new_appended += 1
                except DuplicateFillError:
                    pass

            session_delta = snap.equity - session_start_equity
            ledger_session_pnl = sum(
                (f.realized_pnl_usd or 0) - f.fee
                for f in ledger.fills_by_exchange(exchange)
                if f.ts >= session_start
            )
            drift = session_delta - ledger_session_pnl

            logger.info(
                f"[{exchange}] equity=${snap.equity:.4f} "
                f"Δsession=${session_delta:+.4f} "
                f"ledger_pnl=${ledger_session_pnl:+.4f} "
                f"drift=${drift:+.4f} "
                f"positions={len(snap.positions)} "
                f"new_fills={new_appended}"
            )

            if abs(drift) > max(1.00, 0.02 * abs(snap.equity)):
                logger.error(f"[{exchange}] DRIFT_ALARM: ${drift:+.4f}")

            last_snap_ts = snap.ts
        except Exception as e:
            logger.error(f"[{exchange}] cycle error: {e}", exc_info=True)

        await asyncio.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", required=True, choices=["paradex", "nado", "hibachi"])
    ap.add_argument("--interval", type=int, default=300)
    args = ap.parse_args()
    asyncio.run(run(args.exchange, args.interval))


if __name__ == "__main__":
    main()
