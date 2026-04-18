#!/usr/bin/env python3
"""
Overnight Autonomous Monitor — Consults Qwen hourly, adjusts strategy.

Runs continuously:
1. Every hour, collects trade performance from JSONL logs
2. Checks exchange equity and positions
3. Sends performance data to Qwen for analysis
4. Qwen recommends parameter adjustments
5. Applies adjustments and restarts bots if needed
6. Logs everything to overnight_monitor.log

Usage:
    python3 scripts/overnight_monitor.py                # Default: 1 hour interval
    python3 scripts/overnight_monitor.py --interval 1800  # 30 min
    python3 scripts/overnight_monitor.py --dry-run       # Log recommendations, don't apply
"""

import argparse
import asyncio
import glob
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import requests

from core.strategies.momentum.exchange_adapter import create_adapter

LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "momentum"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SCRIPT_DIR = Path(__file__).resolve().parent
ENGINE_FILE = Path(__file__).resolve().parent.parent / "core" / "strategies" / "momentum" / "engine.py"
BOT_SCRIPT = SCRIPT_DIR / "momentum_mm.py"

OVERNIGHT_LOG = LOG_DIR / "overnight_monitor.log"
OVERNIGHT_JSONL = LOG_DIR / "overnight_decisions.jsonl"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler(OVERNIGHT_LOG),
        logging.StreamHandler(),
    ],
)
logging.getLogger('urllib3').setLevel(logging.ERROR)
logging.getLogger('aiohttp').setLevel(logging.ERROR)
logging.getLogger('asyncio').setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

EXCHANGES = ["hibachi", "nado", "extended"]


# ─── Trade Stats Collection ─────────────────────────────────────

def collect_trade_stats(since_timestamp: str = None) -> dict:
    """Collect trade stats from JSONL log files.

    Args:
        since_timestamp: ISO timestamp string. Only count trades after this time.
                         If None, count all trades.

    Returns:
        Dict with per-exchange stats and overall summary.
    """
    stats = defaultdict(lambda: {
        'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0,
        'sl': 0, 'tp': 0, 'time': 0,
        'avg_hold_min': 0.0, 'holds': [],
        'avg_score': 0.0, 'scores': [],
        'symbols': defaultdict(lambda: {'trades': 0, 'pnl': 0.0, 'wins': 0}),
    })

    for f in glob.glob(str(LOG_DIR / "*_trades.jsonl")):
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    t = json.loads(line)
                except Exception:
                    continue

                # Skip reconciled (entry_price=0, PnL is bogus)
                if t.get('scoring', '') == 'reconciled' or t.get('entry_price', 0) == 0:
                    continue

                # Time filter
                if since_timestamp and t.get('_timestamp', '') < since_timestamp:
                    continue

                ex = t.get('exchange', 'unknown')
                pnl = t.get('pnl', 0.0)
                sym = t.get('symbol', '?')

                stats[ex]['trades'] += 1
                stats[ex]['pnl'] += pnl
                if pnl > 0:
                    stats[ex]['wins'] += 1
                else:
                    stats[ex]['losses'] += 1

                reason = t.get('exit_reason', '')
                if reason == 'SL':
                    stats[ex]['sl'] += 1
                elif reason == 'TP':
                    stats[ex]['tp'] += 1
                elif reason == 'TIME':
                    stats[ex]['time'] += 1

                hold = t.get('hold_minutes', 0)
                stats[ex]['holds'].append(hold)

                score = t.get('score', 0)
                stats[ex]['scores'].append(score)

                stats[ex]['symbols'][sym]['trades'] += 1
                stats[ex]['symbols'][sym]['pnl'] += pnl
                if pnl > 0:
                    stats[ex]['symbols'][sym]['wins'] += 1

    # Compute averages
    result = {}
    for ex, s in stats.items():
        wr = s['wins'] / s['trades'] * 100 if s['trades'] else 0
        avg_hold = sum(s['holds']) / len(s['holds']) if s['holds'] else 0
        avg_score = sum(s['scores']) / len(s['scores']) if s['scores'] else 0

        # Worst symbols (by PnL)
        worst = sorted(s['symbols'].items(), key=lambda x: x[1]['pnl'])[:5]
        best = sorted(s['symbols'].items(), key=lambda x: x[1]['pnl'], reverse=True)[:5]

        result[ex] = {
            'trades': s['trades'],
            'win_rate': round(wr, 1),
            'pnl': round(s['pnl'], 2),
            'sl_exits': s['sl'],
            'tp_exits': s['tp'],
            'time_exits': s['time'],
            'avg_hold_min': round(avg_hold, 1),
            'avg_score': round(avg_score, 1),
            'worst_symbols': [(sym, round(d['pnl'], 2), d['trades']) for sym, d in worst],
            'best_symbols': [(sym, round(d['pnl'], 2), d['trades']) for sym, d in best],
        }

    return result


async def get_exchange_status() -> dict:
    """Get current equity and positions from all exchanges."""
    status = {}
    for exchange in EXCHANGES:
        try:
            adapter = create_adapter(exchange)
            await adapter.discover_markets()
            equity = await adapter.get_equity()
            positions = await adapter.get_all_positions()
            status[exchange] = {
                'equity': round(equity, 2),
                'positions': len(positions),
                'position_details': [
                    {
                        'symbol': p['symbol'],
                        'side': p['side'],
                        'notional': round(p.get('notional', 0), 2),
                        'upnl': round(p.get('unrealized_pnl', 0), 4),
                    }
                    for p in positions
                ],
            }
        except Exception as e:
            status[exchange] = {'error': str(e)}
            logger.error(f"[{exchange}] Status check failed: {e}")
    return status


def get_current_config() -> dict:
    """Read current per-exchange config from momentum_mm.py."""
    # Read the file and extract config values
    config = {}
    try:
        with open(BOT_SCRIPT) as f:
            content = f.read()

        # Extract per-exchange configs by simple regex
        for exchange in EXCHANGES:
            cfg = {}
            # Find the exchange block
            pattern = rf'if exchange == "{exchange}":|elif exchange == "{exchange}":'
            match = re.search(pattern, content)
            if match:
                # Extract the block after the match (up to next elif/else)
                block_start = match.end()
                block_end = content.find('\n        elif exchange', block_start)
                if block_end == -1:
                    block_end = content.find('\n\n', block_start)
                block = content[block_start:block_end] if block_end > 0 else content[block_start:block_start+500]

                for param in ['offset_bps', 'score_min', 'tp_bps', 'sl_bps',
                              'max_hold_minutes', 'size_usd', 'max_positions']:
                    m = re.search(rf'config\.{param}\s*=\s*([0-9.]+)', block)
                    if m:
                        cfg[param] = float(m.group(1))

            config[exchange] = cfg
    except Exception as e:
        logger.error(f"Failed to read config: {e}")

    return config


# ─── Qwen Consultation ──────────────────────────────────────────

# Model fallback chain (try in order)
LLM_MODELS = [
    "qwen/qwen-2.5-72b-instruct",
    "google/gemini-2.0-flash-001",
    "meta-llama/llama-3.1-70b-instruct",
]


def consult_qwen(trade_stats: dict, exchange_status: dict, current_config: dict,
                 last_adjustments: dict = None) -> dict:
    """Send performance data to LLM and get adjustment recommendations.

    Tries multiple models via OpenRouter with fallback.
    """
    api_key = os.getenv('OPEN_ROUTER')
    if not api_key:
        logger.error("OPEN_ROUTER key not set — cannot consult LLM")
        return {'error': 'no api key'}

    # Build context
    stats_str = ""
    for ex, s in trade_stats.items():
        if isinstance(s, dict) and 'trades' in s:
            stats_str += (
                f"\n{ex.upper()}: {s['trades']} trades, {s['win_rate']}% WR, "
                f"PnL=${s['pnl']:+.2f}, SL={s['sl_exits']}, TP={s['tp_exits']}, "
                f"TIME={s['time_exits']}, avg_hold={s['avg_hold_min']}min, avg_score={s['avg_score']}"
            )
            if s.get('worst_symbols'):
                worst = ', '.join(f"{sym}(${pnl:+.2f}/{n}t)" for sym, pnl, n in s['worst_symbols'][:3])
                stats_str += f"\n  Worst: {worst}"
            if s.get('best_symbols'):
                best = ', '.join(f"{sym}(${pnl:+.2f}/{n}t)" for sym, pnl, n in s['best_symbols'][:3])
                stats_str += f"\n  Best: {best}"

    status_str = ""
    for ex, s in exchange_status.items():
        if 'error' in s:
            status_str += f"\n{ex.upper()}: ERROR - {s['error']}"
        else:
            status_str += f"\n{ex.upper()}: equity=${s['equity']}, positions={s['positions']}"

    config_str = ""
    for ex, cfg in current_config.items():
        if cfg:
            params = ', '.join(f"{k}={v}" for k, v in cfg.items())
            config_str += f"\n{ex.upper()}: {params}"

    adjustments_str = ""
    if last_adjustments:
        adjustments_str = f"\nLast adjustments applied: {json.dumps(last_adjustments, indent=2)}"

    prompt = f"""You are a quant trading strategy advisor. Analyze these momentum bot performance metrics and recommend specific parameter adjustments.

CURRENT EXCHANGE STATUS:{status_str}

TRADE PERFORMANCE (all time):{stats_str}

CURRENT CONFIG:{config_str}
{adjustments_str}
STRATEGY: 5-signal scoring (RSI + MACD + Volume + Price Action + EMA, each 0-1, summed 0-5). Entry on POST_ONLY limit orders. TP/SL exits. Volume gate: require vol_score > 0.

CONTEXT:
- With TP=200bps, SL=150bps: breakeven WR = ~43%
- With TP=150bps, SL=100bps: breakeven WR = ~40%
- Using 15-minute candles from Binance for signals
- Extended has 58 assets (too many?), Hibachi 6, Nado 20
- Nado min notional = $100, so size_usd must stay >= 105
- Lower total equity (~$130 across all) so risk management is critical

RESPOND IN THIS EXACT JSON FORMAT (no other text):
{{
    "analysis": "2-3 sentence summary of what's going wrong",
    "recommendations": {{
        "hibachi": {{
            "score_min": <float>,
            "tp_bps": <float>,
            "sl_bps": <float>,
            "max_hold_minutes": <int>,
            "size_usd": <float>,
            "max_positions": <int>
        }},
        "nado": {{
            "score_min": <float>,
            "tp_bps": <float>,
            "sl_bps": <float>,
            "max_hold_minutes": <int>,
            "size_usd": <float>,
            "max_positions": <int>
        }},
        "extended": {{
            "score_min": <float>,
            "tp_bps": <float>,
            "sl_bps": <float>,
            "max_hold_minutes": <int>,
            "size_usd": <float>,
            "max_positions": <int>
        }}
    }},
    "reasoning": "Why these specific changes"
}}"""

    # Try models in fallback order
    for model in LLM_MODELS:
        try:
            logger.info(f"  Trying model: {model}")
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are an expert quant trader and strategy optimizer. Always respond in valid JSON only."
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 1000,
                    "temperature": 0.3,
                },
                timeout=30,
            )

            data = resp.json()

            # Check for API errors (provider down, etc.)
            if 'error' in data:
                err_msg = data['error'].get('message', str(data['error']))[:100]
                logger.warning(f"  {model} error: {err_msg}")
                continue  # Try next model

            content = data["choices"][0]["message"]["content"].strip()
            logger.info(f"  Got response from {model}")

            # Try to extract JSON from response
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            result = json.loads(content)
            result['_model_used'] = model
            return result

        except json.JSONDecodeError as e:
            logger.error(f"  {model} response not valid JSON: {e}")
            logger.info(f"  Raw: {content[:300]}")
            continue  # Try next model
        except Exception as e:
            logger.warning(f"  {model} failed: {e}")
            continue  # Try next model

    return {'error': 'all models failed'}


# ─── Apply Adjustments ──────────────────────────────────────────

def apply_config_adjustments(recommendations: dict) -> bool:
    """Apply Qwen's recommended config changes to momentum_mm.py.

    Returns True if changes were made (bots need restart).
    """
    try:
        with open(BOT_SCRIPT) as f:
            content = f.read()
    except Exception as e:
        logger.error(f"Failed to read bot script: {e}")
        return False

    original = content
    changed = False

    for exchange, params in recommendations.items():
        if exchange not in EXCHANGES:
            continue
        if not isinstance(params, dict):
            continue

        for param, value in params.items():
            if param not in ['score_min', 'tp_bps', 'sl_bps', 'max_hold_minutes',
                             'size_usd', 'max_positions', 'offset_bps']:
                continue

            # Nado min notional guard
            if exchange == 'nado' and param == 'size_usd' and value < 105:
                logger.warning(f"[{exchange}] Ignoring size_usd={value} (Nado min $100 notional)")
                continue

            # Safety bounds
            if param == 'score_min' and (value < 2.0 or value > 4.5):
                logger.warning(f"[{exchange}] score_min={value} out of safe range [2.0, 4.5]")
                continue
            if param == 'tp_bps' and (value < 50 or value > 500):
                logger.warning(f"[{exchange}] tp_bps={value} out of safe range [50, 500]")
                continue
            if param == 'sl_bps' and (value < 50 or value > 300):
                logger.warning(f"[{exchange}] sl_bps={value} out of safe range [50, 300]")
                continue
            if param == 'max_positions' and (value < 1 or value > 10):
                logger.warning(f"[{exchange}] max_positions={value} out of safe range [1, 10]")
                continue
            if param == 'max_hold_minutes' and (value < 30 or value > 1440):
                logger.warning(f"[{exchange}] max_hold_minutes={value} out of safe range [30, 1440]")
                continue

            # Find and replace the config line in the correct exchange block
            # Format: config.param = value  # comment
            pattern = rf'((?:if|elif) exchange == "{exchange}":.*?config\.{param}\s*=\s*)([0-9.]+)'
            match = re.search(pattern, content, re.DOTALL)
            if match:
                old_val = float(match.group(2))
                if param == 'max_hold_minutes' or param == 'max_positions':
                    new_val_str = str(int(value))
                else:
                    new_val_str = str(float(value))

                if abs(old_val - value) > 0.01:
                    old_line = f"config.{param} = {match.group(2)}"
                    new_line = f"config.{param} = {new_val_str}"
                    # Replace only in the correct exchange block
                    content = content[:match.start(2)] + new_val_str + content[match.end(2):]
                    logger.info(f"  [{exchange}] {param}: {old_val} → {value}")
                    changed = True

    if changed:
        try:
            with open(BOT_SCRIPT, 'w') as f:
                f.write(content)
            logger.info("Config changes written to momentum_mm.py")
        except Exception as e:
            logger.error(f"Failed to write config: {e}")
            # Restore original
            with open(BOT_SCRIPT, 'w') as f:
                f.write(original)
            return False

    return changed


def restart_bots():
    """Kill and restart all momentum bots."""
    logger.info("Restarting all momentum bots...")

    # Kill existing
    try:
        subprocess.run(["pkill", "-f", "momentum_mm"], timeout=5)
        time.sleep(3)
    except Exception:
        pass

    # Restart each exchange
    commands = [
        ("hibachi", "python3 -u scripts/momentum_mm.py --exchange hibachi --assets all --interval 60"),
        ("nado", "python3 -u scripts/momentum_mm.py --exchange nado --assets all --interval 60"),
        ("extended", "python3.11 -u scripts/momentum_mm.py --exchange extended --assets all --interval 60"),
    ]

    for exchange, cmd in commands:
        log_file = LOG_DIR / f"{exchange}_bot.log"
        full_cmd = f"nohup {cmd} > {log_file} 2>&1 &"
        try:
            subprocess.Popen(full_cmd, shell=True, cwd=str(Path(__file__).resolve().parent.parent))
            logger.info(f"  Started {exchange}: {cmd}")
        except Exception as e:
            logger.error(f"  Failed to start {exchange}: {e}")

    # Verify after a few seconds
    time.sleep(5)
    ps = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    for exchange in EXCHANGES:
        running = exchange in ps.stdout and "momentum_mm" in ps.stdout
        logger.info(f"  {exchange}: {'RUNNING' if running else 'NOT FOUND'}")


# ─── Main Loop ───────────────────────────────────────────────────

async def run_cycle(dry_run: bool, last_check_time: str, last_adjustments: dict) -> tuple:
    """Run one monitoring cycle. Returns (new_last_check_time, new_adjustments)."""
    ts = datetime.now(timezone.utc)
    logger.info("=" * 70)
    logger.info(f"OVERNIGHT MONITOR — {ts.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info("=" * 70)

    # 1. Collect trade stats
    logger.info("Collecting trade stats...")
    all_stats = collect_trade_stats()
    recent_stats = collect_trade_stats(since_timestamp=last_check_time)

    for ex, s in all_stats.items():
        logger.info(
            f"  [{ex.upper()}] ALL: {s['trades']}t {s['win_rate']}% WR ${s['pnl']:+.2f}"
        )
    for ex, s in recent_stats.items():
        logger.info(
            f"  [{ex.upper()}] NEW: {s['trades']}t {s['win_rate']}% WR ${s['pnl']:+.2f}"
        )

    # 2. Check exchange status
    logger.info("Checking exchange status...")
    exchange_status = await get_exchange_status()
    total_equity = 0
    for ex, s in exchange_status.items():
        if 'equity' in s:
            total_equity += s['equity']
            logger.info(f"  [{ex.upper()}] Equity: ${s['equity']:.2f} | Positions: {s['positions']}")
        else:
            logger.error(f"  [{ex.upper()}] {s.get('error', 'unknown error')}")
    logger.info(f"  TOTAL EQUITY: ${total_equity:.2f}")

    # 3. Get current config
    current_config = get_current_config()

    # 4. Consult Qwen
    logger.info("Consulting Qwen for strategy adjustments...")
    qwen_response = consult_qwen(
        trade_stats=all_stats,
        exchange_status=exchange_status,
        current_config=current_config,
        last_adjustments=last_adjustments,
    )

    if 'error' in qwen_response:
        logger.error(f"Qwen consultation failed: {qwen_response['error']}")
        if 'raw' in qwen_response:
            logger.info(f"Raw: {qwen_response['raw']}")
    else:
        logger.info(f"Qwen analysis: {qwen_response.get('analysis', 'N/A')}")
        logger.info(f"Qwen reasoning: {qwen_response.get('reasoning', 'N/A')}")

        recommendations = qwen_response.get('recommendations', {})
        for ex, params in recommendations.items():
            if isinstance(params, dict):
                logger.info(f"  [{ex.upper()}] Recommended: {params}")

        # 5. Apply adjustments (unless dry-run)
        if dry_run:
            logger.info("DRY-RUN: Not applying adjustments")
        elif recommendations:
            needs_restart = apply_config_adjustments(recommendations)
            if needs_restart:
                logger.info("Config changed — restarting bots...")
                restart_bots()
                last_adjustments = {
                    'timestamp': ts.isoformat(),
                    'recommendations': recommendations,
                    'analysis': qwen_response.get('analysis', ''),
                }
            else:
                logger.info("No config changes needed (same values)")
                last_adjustments = None
        else:
            logger.info("No recommendations from Qwen")

    # 6. Log decision to JSONL
    record = {
        '_timestamp': ts.isoformat(),
        'total_equity': total_equity,
        'exchange_status': exchange_status,
        'trade_stats_all': all_stats,
        'trade_stats_recent': recent_stats,
        'current_config': current_config,
        'qwen_response': qwen_response,
        'applied': not dry_run and 'recommendations' in qwen_response,
    }
    try:
        with open(OVERNIGHT_JSONL, 'a') as f:
            f.write(json.dumps(record, default=str) + '\n')
    except Exception as e:
        logger.error(f"Failed to write JSONL: {e}")

    return ts.isoformat(), last_adjustments


async def main(interval: int, dry_run: bool):
    """Main monitoring loop."""
    logger.info(f"Overnight Monitor starting — interval={interval}s, dry_run={dry_run}")
    logger.info(f"Logs: {OVERNIGHT_LOG}")
    logger.info(f"Decisions: {OVERNIGHT_JSONL}")

    last_check_time = None
    last_adjustments = None

    # Handle graceful shutdown
    running = True

    def signal_handler(sig, frame):
        nonlocal running
        logger.info("Shutting down overnight monitor...")
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while running:
        try:
            last_check_time, last_adjustments = await run_cycle(
                dry_run=dry_run,
                last_check_time=last_check_time,
                last_adjustments=last_adjustments,
            )
        except Exception as e:
            logger.error(f"Cycle error: {e}", exc_info=True)

        logger.info(f"Next check in {interval}s ({interval/60:.0f}min)")
        # Sleep in small increments for graceful shutdown
        for _ in range(interval):
            if not running:
                break
            await asyncio.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Overnight Autonomous Monitor")
    parser.add_argument(
        "--interval", type=int, default=3600,
        help="Check interval in seconds (default: 3600 = 1 hour)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log recommendations without applying them"
    )
    args = parser.parse_args()
    asyncio.run(main(args.interval, args.dry_run))
