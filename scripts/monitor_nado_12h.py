#!/usr/bin/env python3
"""
Nado Bot 12-Hour Monitor — watches nado bot health and auto-fixes issues.

Checks every 5 minutes:
1. Is the nado bot process alive? If dead, restart it.
2. Is equity critically low (< $5)? Alert.
3. Is self-learning blocking everything? Log warning.
4. Is the bot stuck (no log output for 10+ min)? Restart.
5. Are buying power errors spamming? Log the bottleneck.
6. Log equity, positions, self-learning stats each check.

Usage:
    python3 scripts/monitor_nado_12h.py
    # Or background:
    nohup python3 -u scripts/monitor_nado_12h.py > logs/momentum/nado_monitor.log 2>&1 &
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Config
CHECK_INTERVAL = 300  # 5 minutes
DURATION_HOURS = 12
BOT_LOG = Path("logs/momentum/nado_bot.log")
MONITOR_LOG = Path("logs/momentum/nado_monitor.log")
RESTART_CMD = [
    sys.executable, "-u", "scripts/momentum_mm.py",
    "--exchange", "nado", "--assets", "all", "--interval", "60"
]
STALE_THRESHOLD_MINUTES = 10
MIN_EQUITY_ALERT = 5.0

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {level} | NADO-MONITOR | {msg}"
    print(line, flush=True)

def find_nado_pid():
    """Find the running nado momentum bot PID."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "momentum_mm.py.*nado"],
            capture_output=True, text=True
        )
        pids = result.stdout.strip().split('\n')
        pids = [p for p in pids if p.strip()]
        return int(pids[0]) if pids else None
    except Exception:
        return None

def is_process_alive(pid):
    """Check if a PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False

def get_log_age_minutes():
    """How many minutes since the last log line was written."""
    try:
        mtime = BOT_LOG.stat().st_mtime
        age = time.time() - mtime
        return age / 60
    except Exception:
        return 999

def get_last_log_lines(n=50):
    """Get last N lines of the bot log."""
    try:
        result = subprocess.run(
            ["tail", f"-{n}", str(BOT_LOG)],
            capture_output=True, text=True
        )
        return result.stdout
    except Exception:
        return ""

def parse_equity_from_log(lines):
    """Extract latest equity from log."""
    for line in reversed(lines.split('\n')):
        if 'equity=' in line and 'HOURLY' in line:
            try:
                eq_str = line.split('equity=$')[1].split()[0]
                return float(eq_str)
            except Exception:
                pass
    return None

def parse_positions_from_log(lines):
    """Extract current position count from log."""
    for line in reversed(lines.split('\n')):
        if 'positions=' in line and 'HOURLY' in line:
            try:
                pos_str = line.split('positions=')[1].split()[0]
                return int(pos_str)
            except Exception:
                pass
    return None

def count_buying_power_errors(lines):
    """Count buying power errors in recent log."""
    return lines.count("Buying power")

def count_self_learn_blocks(lines):
    """Count self-learning blocks in recent log."""
    return lines.count("SELF-LEARN BLOCKED")

def count_entries(lines):
    """Count entry attempts in recent log."""
    return lines.count("ENTRY:")

def count_fills(lines):
    """Count order fills in recent log."""
    return lines.count("ORDER FILLED")

def restart_nado():
    """Kill and restart the nado bot."""
    pid = find_nado_pid()
    if pid:
        log(f"Killing existing nado bot (PID {pid})", "WARN")
        try:
            os.kill(pid, 9)
            time.sleep(2)
        except Exception as e:
            log(f"Kill failed: {e}", "ERROR")

    log("Starting nado bot...", "WARN")
    proc = subprocess.Popen(
        RESTART_CMD,
        stdout=open(str(BOT_LOG), 'w'),
        stderr=subprocess.STDOUT,
        start_new_session=True
    )
    log(f"Nado bot restarted (PID {proc.pid})", "WARN")
    return proc.pid

def check_self_learning_state():
    """Parse the self-learning seed line from log to get bucket stats."""
    try:
        result = subprocess.run(
            ["grep", "SELF-LEARN.*Seeded", str(BOT_LOG)],
            capture_output=True, text=True
        )
        line = result.stdout.strip().split('\n')[-1] if result.stdout.strip() else ""
        if "Score buckets:" in line:
            bucket_json = line.split("Score buckets: ")[1]
            return json.loads(bucket_json)
    except Exception:
        pass
    return {}

def main():
    log("=" * 60)
    log(f"NADO 12-HOUR MONITOR STARTING")
    log(f"Duration: {DURATION_HOURS}h | Check interval: {CHECK_INTERVAL}s")
    log(f"Bot log: {BOT_LOG}")
    log("=" * 60)

    end_time = datetime.now() + timedelta(hours=DURATION_HOURS)
    check_count = 0
    restart_count = 0
    last_equity = None

    while datetime.now() < end_time:
        check_count += 1
        log(f"--- Check #{check_count} ---")

        pid = find_nado_pid()
        alive = is_process_alive(pid) if pid else False
        log_age = get_log_age_minutes()
        recent_log = get_last_log_lines(200)

        # 1. Process alive check
        if not alive:
            log(f"NADO BOT IS DEAD! Restarting...", "CRITICAL")
            restart_nado()
            restart_count += 1
            time.sleep(10)
            continue

        # 2. Stale log check (bot alive but not writing logs)
        if log_age > STALE_THRESHOLD_MINUTES:
            log(f"Bot log stale ({log_age:.1f} min since last write). Restarting...", "CRITICAL")
            restart_nado()
            restart_count += 1
            time.sleep(10)
            continue

        # 3. Equity check
        equity = parse_equity_from_log(recent_log)
        positions = parse_positions_from_log(recent_log)
        if equity is not None:
            delta = ""
            if last_equity is not None:
                d = equity - last_equity
                delta = f" ({'+' if d >= 0 else ''}{d:.2f} since last check)"
            log(f"Equity: ${equity:.2f}{delta} | Positions: {positions}")
            last_equity = equity

            if equity < MIN_EQUITY_ALERT:
                log(f"EQUITY CRITICALLY LOW: ${equity:.2f}", "CRITICAL")

        # 4. Activity stats
        bp_errors = count_buying_power_errors(recent_log)
        sl_blocks = count_self_learn_blocks(recent_log)
        entries = count_entries(recent_log)
        fills = count_fills(recent_log)

        log(f"Activity: entries={entries}, fills={fills}, bp_errors={bp_errors}, self_learn_blocks={sl_blocks}")

        if bp_errors > 20:
            log(f"HIGH BUYING POWER ERRORS ({bp_errors}) — equity may be too low for $100 min notional", "WARN")

        if sl_blocks > 10:
            log(f"SELF-LEARNING BLOCKING MANY TRADES ({sl_blocks}) — may be too aggressive", "WARN")

        # 5. Self-learning state
        buckets = check_self_learning_state()
        if buckets:
            log(f"Self-learning buckets: {json.dumps(buckets)}")

        # 6. Process info
        log(f"PID: {pid} | Log age: {log_age:.1f}min | Restarts: {restart_count}")
        remaining = (end_time - datetime.now()).total_seconds() / 3600
        log(f"Time remaining: {remaining:.1f}h")

        time.sleep(CHECK_INTERVAL)

    log("=" * 60)
    log(f"12-HOUR MONITOR COMPLETE")
    log(f"Total checks: {check_count} | Restarts: {restart_count}")
    log(f"Final equity: ${last_equity:.2f}" if last_equity else "Final equity: unknown")
    log("=" * 60)

if __name__ == "__main__":
    os.chdir("/Users/admin/Documents/Projects/pacifica-trading-bot")
    main()
