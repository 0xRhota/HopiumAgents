#!/usr/bin/env python3
"""
Grid MM Monitor - Tracks Nado and Paradex for 1 hour
Checks every 5 minutes for:
- Dynamic spread adjustments
- Fill activity
- Position changes
"""

import os
import re
import time
from datetime import datetime, timedelta

LOG_FILE = "logs/grid_mm_monitor.log"
NADO_LOG = "logs/grid_mm_nado.log"
PARADEX_LOG = "logs/grid_mm_live.log"

def log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    line = f"{timestamp} | {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def get_last_status(log_file, lines=30):
    """Extract last status from log file"""
    try:
        with open(log_file, "r") as f:
            content = f.readlines()[-lines:]
        return "".join(content)
    except:
        return ""

def parse_nado_status(content):
    """Parse Nado log for key metrics"""
    # Find last status line with ROC and Spread
    matches = re.findall(r'\$[\d,]+\.\d+ \| ROC: ([+-]?\d+\.\d+)bps \| Spread: (\d+\.\d+)bps', content)
    fills = re.findall(r'Fills: (\d+)', content)
    volume = re.findall(r'Volume: \$(\d+\.\d+)', content)
    position = re.findall(r'Position: ([\d.]+)', content)

    if matches:
        roc, spread = matches[-1]
        return {
            'roc': float(roc),
            'spread': float(spread),
            'fills': int(fills[-1]) if fills else 0,
            'volume': float(volume[-1]) if volume else 0,
            'position': float(position[-1]) if position else 0
        }
    return None

def parse_paradex_status(content):
    """Parse Paradex log for key metrics"""
    # Find last status line
    matches = re.findall(r'Bot: (\d+\.\d+)bps \| ROC: ([+-]?\d+\.\d+)bps', content)
    fills = re.findall(r'Fills: (\d+)', content)
    volume = re.findall(r'Volume: \$(\d+\.\d+)', content)
    position = re.findall(r'Position: ([\d.]+) BTC', content)

    if matches:
        spread, roc = matches[-1]
        return {
            'spread': float(spread),
            'roc': float(roc),
            'fills': int(fills[-1]) if fills else 0,
            'volume': float(volume[-1]) if volume else 0,
            'position': float(position[-1]) if position else 0
        }
    return None

def main():
    log("=" * 60)
    log("GRID MM MONITOR - 1 HOUR")
    log("Tracking: Nado (ETH) + Paradex (BTC)")
    log("=" * 60)

    start_time = datetime.now()
    end_time = start_time + timedelta(hours=1)
    check_interval = 300  # 5 minutes

    # Track initial state
    prev_nado_fills = 0
    prev_paradex_fills = 0
    prev_nado_spread = 0
    prev_paradex_spread = 0

    check_num = 0

    while datetime.now() < end_time:
        check_num += 1
        elapsed = (datetime.now() - start_time).total_seconds() / 60
        remaining = (end_time - datetime.now()).total_seconds() / 60

        log("")
        log(f"=== CHECK #{check_num} ({elapsed:.0f}m elapsed, {remaining:.0f}m remaining) ===")

        # Parse Nado
        nado_content = get_last_status(NADO_LOG)
        nado = parse_nado_status(nado_content)

        if nado:
            spread_change = ""
            if prev_nado_spread and nado['spread'] != prev_nado_spread:
                spread_change = f" (CHANGED from {prev_nado_spread}!)"

            fill_change = ""
            if nado['fills'] > prev_nado_fills:
                fill_change = f" (+{nado['fills'] - prev_nado_fills} NEW FILLS!)"

            log(f"NADO: ROC={nado['roc']:+.1f}bps | Spread={nado['spread']}bps{spread_change} | Fills={nado['fills']}{fill_change} | Vol=${nado['volume']:.2f}")

            prev_nado_fills = nado['fills']
            prev_nado_spread = nado['spread']
        else:
            log("NADO: Could not parse status")

        # Parse Paradex
        paradex_content = get_last_status(PARADEX_LOG)
        paradex = parse_paradex_status(paradex_content)

        if paradex:
            spread_change = ""
            if prev_paradex_spread and paradex['spread'] != prev_paradex_spread:
                spread_change = f" (CHANGED from {prev_paradex_spread}!)"

            fill_change = ""
            if paradex['fills'] > prev_paradex_fills:
                fill_change = f" (+{paradex['fills'] - prev_paradex_fills} NEW FILLS!)"

            log(f"PARADEX: ROC={paradex['roc']:+.1f}bps | Spread={paradex['spread']}bps{spread_change} | Fills={paradex['fills']}{fill_change} | Vol=${paradex['volume']:.2f}")

            prev_paradex_fills = paradex['fills']
            prev_paradex_spread = paradex['spread']
        else:
            log("PARADEX: Could not parse status")

        # Summary
        if nado and paradex:
            if nado['fills'] == 0 and paradex['fills'] == 0:
                log("⚠️  Both bots have 0 fills - investigating...")
                # Check if it's an inventory issue
                if nado['position'] >= 0.02:
                    log("   NADO: 100% inventory (LONG) - can only SELL, waiting for buyers")

        # Wait for next check
        if datetime.now() < end_time:
            log(f"Next check in {check_interval//60} minutes...")
            time.sleep(check_interval)

    log("")
    log("=" * 60)
    log("MONITORING COMPLETE")
    log(f"Final Stats:")
    log(f"  Nado Fills: {prev_nado_fills}")
    log(f"  Paradex Fills: {prev_paradex_fills}")
    log("=" * 60)

if __name__ == "__main__":
    os.chdir("/Users/admin/Documents/Projects/pacifica-trading-bot")
    main()
