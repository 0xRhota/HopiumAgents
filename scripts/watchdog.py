#!/usr/bin/env python3
"""
Watchdog Monitor v3 — 12-hour autonomous monitoring with Qwen consultation.

QUERIES EXCHANGE APIs DIRECTLY for positions and equity. No log-parsing guesswork.

Every 30 minutes:
  1. Check all 3 momentum bots are alive
  2. Query each exchange API for real equity + positions
  3. Parse logs only for scores and errors
  4. Collect recent trades from JSONL files
  5. Send full status report to Qwen for analysis
  6. Save Qwen's report + raw data to watchdog_reports.jsonl
  7. Append actionable findings to LEARNINGS.md

Does NOT: modify bot configs, restart bots, or auto-tune parameters.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── Paths ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = PROJECT_ROOT / "logs" / "momentum"
REPORT_FILE = LOG_DIR / "watchdog_reports.jsonl"
LEARNINGS_FILE = PROJECT_ROOT / "LEARNINGS.md"

load_dotenv(PROJECT_ROOT / ".env")
OPENROUTER_KEY = os.getenv("OPEN_ROUTER", "")

from core.strategies.momentum.exchange_adapter import create_adapter

# ── Config ───────────────────────────────────────────────────────────
CHECK_INTERVAL = 1800  # 30 minutes
DURATION_HOURS = 12
EXCHANGES = ["hibachi", "nado", "extended"]


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} | WATCHDOG | {msg}", flush=True)


# ── Exchange API queries (source of truth) ───────────────────────────

async def query_exchange(exchange: str) -> dict:
    """Query exchange API directly for equity and positions."""
    result = {"exchange": exchange, "equity": None, "positions": [], "api_error": None}
    try:
        adapter = create_adapter(exchange)
        # Get real equity
        equity = await adapter.get_equity()
        result["equity"] = round(equity, 2)

        # Get real positions
        positions = await adapter.get_all_positions()
        for p in positions:
            result["positions"].append({
                "symbol": p.get("symbol", "?"),
                "side": p.get("side", "?"),
                "size": p.get("size", 0),
                "entry_price": p.get("entry_price", 0),
                "notional": p.get("notional", 0),
            })
    except Exception as e:
        result["api_error"] = str(e)
    return result


def query_exchange_sync(exchange: str) -> dict:
    """Sync wrapper for async exchange query."""
    try:
        return asyncio.get_event_loop().run_until_complete(query_exchange(exchange))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(query_exchange(exchange))
        finally:
            loop.close()


# ── Process checks ───────────────────────────────────────────────────

BOT_PROCESSES = {
    "hibachi": "momentum_mm.py --exchange hibachi",
    "nado":    "momentum_mm.py --exchange nado",
    "extended": "momentum_mm.py --exchange extended",
}


def check_bot_alive(exchange: str) -> dict:
    """Check if a momentum bot process is running."""
    pattern = BOT_PROCESSES[exchange]
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=5
        )
        pids = result.stdout.strip().split("\n") if result.stdout.strip() else []
        return {"alive": len(pids) > 0, "pids": pids}
    except Exception as e:
        return {"alive": False, "pids": [], "error": str(e)}


# ── Log parsing (scores + errors only — NOT positions/equity) ────────

def get_recent_log_lines(exchange: str, n: int = 200) -> str:
    """Get the last N lines from bot log."""
    log_file = LOG_DIR / f"{exchange}_bot.log"
    if not log_file.exists():
        return ""
    try:
        result = subprocess.run(
            ["tail", f"-{n}", str(log_file)],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout
    except Exception:
        return ""


def parse_log_scores_and_errors(log_text: str) -> dict:
    """Extract scores and errors from recent log lines. NOT positions/equity."""
    result = {"scores": [], "errors": [], "last_activity": None}

    for line in log_text.split("\n"):
        line = line.strip()
        if not line:
            continue

        ts_match = re.match(r"(\d{2}:\d{2}:\d{2})", line)

        # Score lines
        score_match = re.search(
            r"\|(\w+)\] Score=([\d.]+)/5\.0 \[(.+?)\] → (\w+) RSI=([\d.]+) vol=([\d.]+)x",
            line
        )
        if score_match:
            result["scores"].append({
                "symbol": score_match.group(1),
                "score": float(score_match.group(2)),
                "breakdown": score_match.group(3),
                "direction": score_match.group(4),
                "rsi": float(score_match.group(5)),
                "vol": float(score_match.group(6)),
            })
            if ts_match:
                result["last_activity"] = ts_match.group(1)

        # Real errors only
        if any(kw in line.lower() for kw in ["error", "exception", "traceback"]):
            if not any(skip in line.lower() for skip in [
                "cancel", "urllib3", "exit:", "signal-based", "emergency"
            ]):
                result["errors"].append(line[:200])

        # Equity too low — track once
        if "too low for min notional" in line:
            if not any("too low" in e for e in result["errors"]):
                result["errors"].append(line[:200])

    # Deduplicate scores — keep only latest per symbol
    seen = {}
    for s in result["scores"]:
        seen[s["symbol"]] = s
    result["scores"] = list(seen.values())

    return result


# ── Trade data collection ────────────────────────────────────────────

def get_recent_trades(exchange: str, hours: int = 1) -> list:
    """Get trades from the last N hours from JSONL files."""
    trades = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen_ids = set()

    for f in LOG_DIR.glob(f"{exchange}*_trades.jsonl"):
        try:
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        trade = json.loads(line)
                        tid = trade.get("id", "")
                        if tid in seen_ids:
                            continue
                        seen_ids.add(tid)
                        ts = trade.get("_timestamp", "")
                        if ts:
                            trade_time = datetime.fromisoformat(ts)
                            if trade_time >= cutoff:
                                trades.append(trade)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except Exception:
            continue

    return sorted(trades, key=lambda t: t.get("_timestamp", ""))


def get_all_trades_today(exchange: str) -> list:
    """Get all trades from today."""
    trades = []
    today = datetime.now(timezone.utc).date()
    seen_ids = set()

    for f in LOG_DIR.glob(f"{exchange}*_trades.jsonl"):
        try:
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        trade = json.loads(line)
                        tid = trade.get("id", "")
                        if tid in seen_ids:
                            continue
                        seen_ids.add(tid)
                        ts = trade.get("_timestamp", "")
                        if ts:
                            trade_time = datetime.fromisoformat(ts)
                            if trade_time.date() >= today:
                                trades.append(trade)
                    except (json.JSONDecodeError, ValueError):
                        continue
        except Exception:
            continue

    return sorted(trades, key=lambda t: t.get("_timestamp", ""))


# ── Qwen consultation ────────────────────────────────────────────────

def consult_qwen(status_report: str) -> str:
    """Send status report to Qwen and get analysis."""
    if not OPENROUTER_KEY:
        return "ERROR: No OPEN_ROUTER key found in .env"

    system_prompt = """You are an expert quant trader monitoring a live multi-exchange perpetual futures trading system.

The system uses a 5-signal momentum scoring engine:
- RSI(14), MACD(12,26,9), Volume(20-period avg), Price Action (S/R), EMA(8/21)
- Each signal 0-1, summed to 0-5.0 total
- Trades only when score >= 3.0 with direction confluence
- Signal-based exits: TREND_FLIP (opposite direction with score >= 2.0) or EMERGENCY_SL (5% catastrophic stop)
- Dynamic position sizing: 20% of account equity per position
- Volume gate: requires volume signal > 0 to enter

You are reviewing the current bot status. The positions and equity shown are REAL — queried directly from exchange APIs.

Provide:
1. HEALTH CHECK: Are all bots running correctly? Any errors or concerns?
2. MARKET READ: What are RSI/volume conditions telling us? Is the market trending or ranging?
3. POSITION REVIEW: Are current positions well-placed? Any that should be watched closely?
4. TRADE ANALYSIS: Review recent trades — what worked, what didn't?
5. ACTIONABLE INSIGHT: One specific, concrete recommendation (not parameter changes — the system is signal-based now).

Keep response under 400 words. Be direct and specific."""

    models = [
        "qwen/qwen-2.5-72b-instruct",
        "google/gemini-2.0-flash-001",
    ]
    for model in models:
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": status_report},
                    ],
                    "max_tokens": 600,
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            prefix = "" if model == models[0] else f"[{model} fallback] "
            return prefix + data["choices"][0]["message"]["content"]
        except Exception as e:
            log(f"  {model} failed: {e}")
            continue

    return "ERROR: All LLM providers failed"


# ── Report building ──────────────────────────────────────────────────

def build_status_report(cycle: int) -> tuple:
    """Build complete status report. Returns (report_text, raw_data)."""
    now = datetime.now()
    raw = {"cycle": cycle, "timestamp": now.isoformat(), "exchanges": {}}

    lines = []
    lines.append(f"=== WATCHDOG CYCLE #{cycle} — {now.strftime('%Y-%m-%d %H:%M')} ===\n")

    for exchange in EXCHANGES:
        lines.append(f"\n--- {exchange.upper()} ---")

        # 1. Process check
        proc = check_bot_alive(exchange)
        raw[f"{exchange}_alive"] = proc["alive"]
        if proc["alive"]:
            lines.append(f"Process: RUNNING (PIDs: {', '.join(proc['pids'])})")
        else:
            lines.append("Process: DOWN!")

        # 2. REAL equity + positions from exchange API
        log(f"  Querying {exchange} API...")
        api_data = query_exchange_sync(exchange)
        raw["exchanges"][exchange] = api_data

        if api_data["api_error"]:
            lines.append(f"API ERROR: {api_data['api_error']}")
        else:
            lines.append(f"Equity: ${api_data['equity']:.2f} (from API)")

            if api_data["positions"]:
                lines.append(f"Positions ({len(api_data['positions'])}):")
                for p in api_data["positions"]:
                    notional_str = f" ${p['notional']:.2f}" if p.get("notional") else ""
                    entry_str = f" entry=${p['entry_price']:.4f}" if p.get("entry_price") else ""
                    lines.append(
                        f"  {p['side']} {p['symbol']} size={p['size']}{notional_str}{entry_str}"
                    )
            else:
                lines.append("Positions: NONE")

        # 3. Scores + errors from log (this is fine from logs)
        log_text = get_recent_log_lines(exchange, 200)
        log_data = parse_log_scores_and_errors(log_text)

        if log_data["scores"]:
            lines.append("Recent scores:")
            for s in log_data["scores"][-6:]:
                lines.append(
                    f"  {s['symbol']}: {s['score']}/5.0 [{s['breakdown']}] "
                    f"→ {s['direction']} RSI={s['rsi']} vol={s['vol']}x"
                )

        if log_data["errors"]:
            lines.append(f"Errors ({len(log_data['errors'])}):")
            for err in log_data["errors"][-5:]:
                lines.append(f"  ! {err[:150]}")

        if log_data["last_activity"]:
            lines.append(f"Last activity: {log_data['last_activity']}")

        # 4. Recent trades from JSONL
        recent = get_recent_trades(exchange, hours=1)
        today = get_all_trades_today(exchange)
        raw[f"{exchange}_recent_trades"] = len(recent)
        raw[f"{exchange}_today_trades"] = len(today)

        def _tpnl(t):
            # Prefer calc-based pnl; fall back to legacy pnl_delta field for old records.
            v = t.get("pnl")
            if v is None:
                v = t.get("pnl_delta", 0)
            return v or 0

        if recent:
            lines.append(f"Trades last hour: {len(recent)}")
            for t in recent[-3:]:
                flag = " [RECON]" if t.get("reconciled") else ""
                lines.append(
                    f"  {t['side']} {t['symbol']} PnL=${_tpnl(t):.4f}{flag} "
                    f"hold={t.get('hold_minutes', 0):.1f}min exit={t.get('exit_reason', '?')}"
                )

        if today:
            real = [t for t in today if not t.get("reconciled") and t.get("pnl") is not None]
            total_pnl = sum(_tpnl(t) for t in real)
            wins = sum(1 for t in real if _tpnl(t) > 0)
            wr = (wins / len(real) * 100) if real else 0
            skipped = len(today) - len(real)
            extra = f" (+{skipped} reconciled/unknown skipped)" if skipped else ""
            lines.append(
                f"Today: {len(real)} trades, {wr:.0f}% WR, "
                f"PnL=${total_pnl:.4f}{extra}"
            )

    report = "\n".join(lines)
    return report, raw


# ── Save report ──────────────────────────────────────────────────────

def save_report(cycle: int, report_text: str, qwen_analysis: str, raw_data: dict):
    """Append report to JSONL file."""
    record = {
        "cycle": cycle,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "report": report_text,
        "qwen_analysis": qwen_analysis,
        "raw": raw_data,
    }
    with open(REPORT_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def append_learning(finding: str):
    """Append an actionable finding to LEARNINGS.md."""
    if not finding or len(finding) < 20:
        return
    try:
        with open(LEARNINGS_FILE, "a") as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            f.write(f"\n### WATCHDOG [{ts}]\n{finding}\n")
    except Exception:
        pass


# ── Main loop ────────────────────────────────────────────────────────

def main():
    log(f"Starting {DURATION_HOURS}-hour monitoring ({CHECK_INTERVAL}s interval)")
    log(f"Exchanges: {', '.join(EXCHANGES)}")
    log(f"Reports: {REPORT_FILE}")
    log("Using EXCHANGE APIs for positions/equity (not log parsing)")

    end_time = datetime.now() + timedelta(hours=DURATION_HOURS)
    cycle = 0

    while datetime.now() < end_time:
        cycle += 1
        remaining_hrs = (end_time - datetime.now()).total_seconds() / 3600
        log(f"--- Cycle #{cycle} ({remaining_hrs:.1f}h remaining) ---")

        try:
            # 1. Build status report (queries exchange APIs)
            report_text, raw_data = build_status_report(cycle)
            log("Status collected from APIs")

            # 2. Check for dead bots
            for exchange in EXCHANGES:
                if not raw_data.get(f"{exchange}_alive", True):
                    log(f"WARNING: {exchange} bot is DOWN!")

            # 3. Consult Qwen
            log("Consulting Qwen...")
            qwen_analysis = consult_qwen(report_text)
            log(f"Qwen responded ({len(qwen_analysis)} chars)")

            # 4. Save report
            save_report(cycle, report_text, qwen_analysis, raw_data)
            log(f"Report saved to {REPORT_FILE}")

            # 5. Print summary
            print(f"\n{'='*60}")
            print(report_text)
            print(f"\n--- QWEN ANALYSIS ---")
            print(qwen_analysis)
            print(f"{'='*60}\n")

            # 6. Extract actionable finding
            for qline in qwen_analysis.split("\n"):
                if any(kw in qline.upper() for kw in ["ACTIONABLE", "RECOMMEND", "WATCH", "CONCERN"]):
                    append_learning(qline.strip())
                    break

        except Exception as e:
            log(f"ERROR in cycle #{cycle}: {e}")
            import traceback
            traceback.print_exc()

        # Sleep until next cycle
        remaining = (end_time - datetime.now()).total_seconds()
        sleep_time = min(CHECK_INTERVAL, max(0, remaining))
        if sleep_time > 0:
            log(f"Sleeping {sleep_time/60:.0f}min until next check...")
            time.sleep(sleep_time)

    log(f"Monitoring complete — {cycle} cycles over {DURATION_HOURS} hours")


if __name__ == "__main__":
    main()
