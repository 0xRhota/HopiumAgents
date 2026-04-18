#!/usr/bin/env python3
"""
Exchange Monitor — Continuous visibility into all exchange activity.

Checks all exchanges every N minutes and logs:
- Account equity
- Open positions (symbol, side, size, entry, PnL)
- Bot process status (running/dead)
- Alerts on anomalies (bot death, unexpected positions)

Usage:
    python3 scripts/monitor.py                  # Default: 5 min interval
    python3 scripts/monitor.py --interval 120   # 2 min interval
    python3 scripts/monitor.py --once           # Single check, no loop
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from core.strategies.momentum.exchange_adapter import create_adapter

LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "momentum"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MONITOR_LOG = LOG_DIR / "monitor.log"
MONITOR_JSONL = LOG_DIR / "monitor_snapshots.jsonl"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler(MONITOR_LOG),
        logging.StreamHandler(),
    ],
)
logging.getLogger('urllib3').setLevel(logging.ERROR)
logging.getLogger('aiohttp').setLevel(logging.ERROR)
logging.getLogger('asyncio').setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

EXCHANGES = ["hibachi", "nado", "extended"]


def check_bot_processes() -> dict:
    """Check which bot processes are running."""
    result = {}
    try:
        ps = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        )
        lines = ps.stdout.splitlines()
        for exchange in EXCHANGES:
            pids = []
            for line in lines:
                if "momentum_mm" in line and exchange in line and "grep" not in line:
                    parts = line.split()
                    if len(parts) > 1:
                        pids.append(parts[1])
            result[exchange] = pids
    except Exception as e:
        logger.error(f"Process check failed: {e}")
    return result


async def check_exchange(exchange: str, adapters: dict) -> dict:
    """Check equity and positions on one exchange."""
    snapshot = {
        "exchange": exchange,
        "equity": 0.0,
        "positions": [],
        "position_count": 0,
        "total_notional": 0.0,
        "total_upnl": 0.0,
        "error": None,
    }

    try:
        if exchange not in adapters:
            adapters[exchange] = create_adapter(exchange)
            # Discover markets for proper size increments
            await adapters[exchange].discover_markets()
        adapter = adapters[exchange]
        equity = await adapter.get_equity()
        snapshot["equity"] = round(equity, 4)

        positions = await adapter.get_all_positions()
        snapshot["position_count"] = len(positions)

        for p in positions:
            snapshot["positions"].append({
                "symbol": p["symbol"],
                "side": p["side"],
                "size": p["size"],
                "entry": p.get("entry_price", 0),
                "notional": round(p.get("notional", 0), 2),
                "upnl": round(p.get("unrealized_pnl", 0), 4),
            })
            snapshot["total_notional"] += p.get("notional", 0)
            snapshot["total_upnl"] += p.get("unrealized_pnl", 0)

        snapshot["total_notional"] = round(snapshot["total_notional"], 2)
        snapshot["total_upnl"] = round(snapshot["total_upnl"], 4)

    except Exception as e:
        snapshot["error"] = str(e)
        logger.error(f"[{exchange.upper()}] Check failed: {e}")

    return snapshot


async def run_check():
    """Run a single monitoring check across all exchanges."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    logger.info("=" * 70)
    logger.info(f"MONITOR CHECK — {ts}")
    logger.info("=" * 70)

    # Check bot processes
    procs = check_bot_processes()
    for exchange in EXCHANGES:
        pids = procs.get(exchange, [])
        status = f"RUNNING (PID {', '.join(pids)})" if pids else "DEAD"
        if not pids:
            logger.warning(f"  [{exchange.upper()}] Bot: {status}")
        else:
            logger.info(f"  [{exchange.upper()}] Bot: {status}")

    # Check each exchange (sequential to share adapters and avoid rate limits)
    adapters = getattr(run_check, '_adapters', {})
    snapshots = []
    for ex in EXCHANGES:
        try:
            snap = await check_exchange(ex, adapters)
            snapshots.append(snap)
        except Exception as e:
            snapshots.append(e)
    run_check._adapters = adapters

    total_equity = 0.0
    total_positions = 0
    total_notional = 0.0

    for snap in snapshots:
        if isinstance(snap, Exception):
            logger.error(f"  Exchange check exception: {snap}")
            continue

        ex = snap["exchange"].upper()
        eq = snap["equity"]
        total_equity += eq
        total_positions += snap["position_count"]
        total_notional += snap["total_notional"]

        if snap["error"]:
            logger.error(f"  [{ex}] ERROR: {snap['error']}")
            continue

        logger.info(f"  [{ex}] Equity: ${eq:.2f} | Positions: {snap['position_count']} | Notional: ${snap['total_notional']:.2f} | uPnL: ${snap['total_upnl']:+.4f}")

        for p in snap["positions"]:
            upnl_str = f" uPnL=${p['upnl']:+.4f}" if p["upnl"] != 0 else ""
            entry_str = f" entry=${p['entry']:.4f}" if p["entry"] > 0 else ""
            logger.info(f"    {p['symbol']:>15} {p['side']:>5} size={p['size']:.6f}{entry_str} notional=${p['notional']:.2f}{upnl_str}")

    logger.info(f"  TOTAL: Equity=${total_equity:.2f} | Positions={total_positions} | Notional=${total_notional:.2f}")

    # Save snapshot to JSONL
    record = {
        "_timestamp": datetime.now(timezone.utc).isoformat(),
        "total_equity": round(total_equity, 4),
        "total_positions": total_positions,
        "total_notional": round(total_notional, 2),
        "processes": {ex: bool(procs.get(ex)) for ex in EXCHANGES},
        "exchanges": [],
    }
    for snap in snapshots:
        if not isinstance(snap, Exception):
            record["exchanges"].append(snap)

    with open(MONITOR_JSONL, "a") as f:
        f.write(json.dumps(record) + "\n")

    # Alerts
    for exchange in EXCHANGES:
        pids = procs.get(exchange, [])
        if not pids:
            logger.warning(f"  ALERT: {exchange.upper()} bot is NOT running!")

    for snap in snapshots:
        if isinstance(snap, Exception):
            continue
        if snap["equity"] < 0:
            logger.warning(f"  ALERT: {snap['exchange'].upper()} equity is NEGATIVE: ${snap['equity']:.2f}")

    logger.info("")


async def main(interval: int, once: bool):
    if once:
        await run_check()
        return

    logger.info(f"Monitor starting — checking every {interval}s")
    while True:
        try:
            await run_check()
        except Exception as e:
            logger.error(f"Monitor error: {e}", exc_info=True)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Exchange Monitor")
    parser.add_argument("--interval", type=int, default=300, help="Check interval in seconds (default: 300 = 5min)")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()
    asyncio.run(main(args.interval, args.once))
