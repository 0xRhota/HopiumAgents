"""
Swing Orchestrator Logging System

CRITICAL: All data must be accurate and verifiable.
- Every cycle logs complete state
- Exchange API is source of truth
- Log both "expected" and "actual" values
"""

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
from pathlib import Path
import logging

from . import config

# Set up file logger
log_dir = Path(config.LOG_DIR)
log_dir.mkdir(exist_ok=True)

# Main logger
logger = logging.getLogger("swing_orchestrator")
logger.setLevel(logging.DEBUG)

# File handler
fh = logging.FileHandler(log_dir / config.LOG_FILE)
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
logger.addHandler(fh)

# Console handler
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
))
logger.addHandler(ch)


def get_timestamp() -> str:
    """Get current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(filename: str, data: Dict) -> None:
    """Append a JSON line to a file."""
    filepath = log_dir / filename
    data["_timestamp"] = get_timestamp()
    with open(filepath, "a") as f:
        f.write(json.dumps(data) + "\n")


def log_cycle_start(cycle_number: int) -> None:
    """Log the start of a decision cycle."""
    logger.info(f"{'='*60}")
    logger.info(f"CYCLE {cycle_number} START")
    logger.info(f"{'='*60}")


def log_cycle_end(cycle_number: int, duration_seconds: float) -> None:
    """Log the end of a decision cycle."""
    logger.info(f"CYCLE {cycle_number} END (duration: {duration_seconds:.2f}s)")
    logger.info(f"{'='*60}")


def log_funding_rates(funding_data: Dict[str, Dict[str, float]]) -> None:
    """
    Log funding rates from all exchanges.

    funding_data format:
    {
        "binance": {"BTC": 0.0001, "ETH": 0.0002},
        "paradex": {"BTC": 0.0001},
        ...
    }
    """
    logger.info("FUNDING RATES:")
    for exchange, rates in funding_data.items():
        for asset, rate in rates.items():
            pct = rate * 100
            signal = ""
            if rate > config.FUNDING_EXTREME_POSITIVE:
                signal = " [STRONG LONG]"
            elif rate > config.FUNDING_MODERATE_POSITIVE:
                signal = " [weak long]"
            elif rate < config.FUNDING_EXTREME_NEGATIVE:
                signal = " [STRONG SHORT]"
            elif rate < config.FUNDING_MODERATE_NEGATIVE:
                signal = " [weak short]"
            logger.info(f"  {exchange}/{asset}: {pct:+.4f}%{signal}")

    _append_jsonl(config.FUNDING_FILE, {
        "type": "funding_rates",
        "data": funding_data
    })


def log_technical_analysis(symbol: str, indicators: Dict, score: float) -> None:
    """
    Log technical indicators and score for a symbol.

    indicators format:
    {
        "rsi": 45.2,
        "macd": {"value": 0.5, "signal": 0.3, "histogram": 0.2},
        "oi_change_pct": 2.5,
        "volume_ratio": 1.8,
        "ema_short": 97000,
        "ema_long": 96500,
        "price": 97200
    }
    """
    logger.info(f"TECHNICAL - {symbol}:")
    logger.info(f"  RSI: {indicators.get('rsi', 'N/A'):.1f}")
    if 'macd' in indicators:
        macd = indicators['macd']
        logger.info(f"  MACD: {macd.get('histogram', 0):.4f} (hist)")
    logger.info(f"  OI Change: {indicators.get('oi_change_pct', 0):+.2f}%")
    logger.info(f"  Volume Ratio: {indicators.get('volume_ratio', 1):.2f}x")
    logger.info(f"  Price: ${indicators.get('price', 0):,.2f}")
    logger.info(f"  SCORE: {score:.2f}/5.0")


def log_decision(decision: Dict) -> None:
    """
    Log a trading decision.

    decision format:
    {
        "action": "SWING" | "SCALP" | "NO_TRADE",
        "direction": "LONG" | "SHORT" | None,
        "symbol": "BTC",
        "exchange": "paradex",
        "conviction": "HIGH" | "STANDARD" | None,
        "size_usd": 20.0,
        "technical_score": 3.5,
        "funding_signal": "STRONG",
        "short_bias_applied": True,
        "reasoning": "..."
    }
    """
    action = decision.get("action", "NO_TRADE")

    if action == "NO_TRADE":
        logger.info(f"DECISION: NO_TRADE - {decision.get('reasoning', 'No signal')}")
    else:
        direction = decision.get("direction", "?")
        symbol = decision.get("symbol", "?")
        exchange = decision.get("exchange", "?")
        conviction = decision.get("conviction", "?")
        size = decision.get("size_usd", 0)

        logger.info(f"DECISION: {action} {direction} {symbol}")
        logger.info(f"  Exchange: {exchange}")
        logger.info(f"  Conviction: {conviction}")
        logger.info(f"  Size: ${size:.2f}")
        logger.info(f"  Tech Score: {decision.get('technical_score', 0):.2f}")
        logger.info(f"  Funding: {decision.get('funding_signal', 'N/A')}")
        logger.info(f"  Short Bias: {'Applied' if decision.get('short_bias_applied') else 'N/A'}")
        logger.info(f"  Reasoning: {decision.get('reasoning', 'N/A')}")

    _append_jsonl(config.DECISIONS_FILE, decision)


def log_execution(result: Dict) -> None:
    """
    Log trade execution result.

    result format:
    {
        "success": True | False,
        "exchange": "paradex",
        "symbol": "BTC",
        "direction": "LONG",
        "size": 0.001,
        "price": 97000,
        "order_id": "...",
        "error": None | "error message"
    }
    """
    if result.get("success"):
        logger.info(f"EXECUTION SUCCESS:")
        logger.info(f"  {result['exchange']}/{result['symbol']} {result['direction']}")
        logger.info(f"  Size: {result.get('size', 0)} @ ${result.get('price', 0):,.2f}")
        logger.info(f"  Order ID: {result.get('order_id', 'N/A')}")
    else:
        logger.error(f"EXECUTION FAILED:")
        logger.error(f"  {result.get('exchange', '?')}/{result.get('symbol', '?')}")
        logger.error(f"  Error: {result.get('error', 'Unknown error')}")

    _append_jsonl(config.DECISIONS_FILE, {
        "type": "execution",
        **result
    })


def log_positions(positions: Dict[str, List[Dict]]) -> None:
    """
    Log current positions from ALL exchanges.
    MUST be from exchange API, not local state.

    positions format:
    {
        "paradex": [
            {"symbol": "BTC", "side": "LONG", "size": 0.001, "entry": 97000, "pnl": 1.5}
        ],
        "hibachi": [],
        ...
    }
    """
    logger.info("POSITIONS (from exchange APIs):")

    total_positions = 0
    total_unrealized_pnl = 0.0

    for exchange, pos_list in positions.items():
        if pos_list:
            for pos in pos_list:
                total_positions += 1
                pnl = pos.get('pnl', 0)
                total_unrealized_pnl += pnl
                logger.info(f"  {exchange}/{pos['symbol']}: {pos['side']} {pos.get('size', 0)} @ ${pos.get('entry', 0):,.2f} (P&L: ${pnl:+.2f})")
        else:
            logger.info(f"  {exchange}: No positions")

    logger.info(f"  TOTAL: {total_positions} positions, ${total_unrealized_pnl:+.2f} unrealized P&L")

    _append_jsonl(config.POSITIONS_FILE, {
        "type": "positions",
        "positions": positions,
        "total_count": total_positions,
        "total_unrealized_pnl": total_unrealized_pnl
    })


def log_balances(balances: Dict[str, float]) -> None:
    """
    Log balances from ALL exchanges.
    MUST be from exchange API, not local state.

    balances format:
    {
        "paradex": 100.50,
        "hibachi": 19.33,
        "nado": 12.06,
        "extended": 5.40
    }
    """
    logger.info("BALANCES (from exchange APIs):")

    total = 0.0
    for exchange, balance in balances.items():
        total += balance
        logger.info(f"  {exchange}: ${balance:.2f}")

    logger.info(f"  TOTAL: ${total:.2f}")

    _append_jsonl(config.PNL_FILE, {
        "type": "balances",
        "balances": balances,
        "total": total
    })


def log_pnl(pnl_data: Dict[str, Dict]) -> None:
    """
    Log P&L from ALL exchanges.
    MUST be from exchange API.

    pnl_data format:
    {
        "paradex": {"realized": 5.0, "unrealized": 1.5, "fees": 0.10},
        ...
    }
    """
    logger.info("P&L (from exchange APIs):")

    total_realized = 0.0
    total_unrealized = 0.0
    total_fees = 0.0

    for exchange, data in pnl_data.items():
        realized = data.get('realized', 0)
        unrealized = data.get('unrealized', 0)
        fees = data.get('fees', 0)

        total_realized += realized
        total_unrealized += unrealized
        total_fees += fees

        logger.info(f"  {exchange}: Realized ${realized:+.2f}, Unrealized ${unrealized:+.2f}, Fees ${fees:.2f}")

    net = total_realized + total_unrealized - total_fees
    logger.info(f"  TOTAL: Realized ${total_realized:+.2f}, Unrealized ${total_unrealized:+.2f}, Fees ${total_fees:.2f}")
    logger.info(f"  NET P&L: ${net:+.2f}")

    _append_jsonl(config.PNL_FILE, {
        "type": "pnl",
        "pnl": pnl_data,
        "total_realized": total_realized,
        "total_unrealized": total_unrealized,
        "total_fees": total_fees,
        "net": net
    })


def log_validation(expected: Dict, actual: Dict, field: str) -> bool:
    """
    Validate expected vs actual data.
    Returns True if within tolerance, False if drift detected.
    """
    for key in expected:
        if key in actual:
            exp_val = expected[key]
            act_val = actual[key]

            if isinstance(exp_val, (int, float)) and isinstance(act_val, (int, float)):
                if exp_val != 0:
                    drift = abs(act_val - exp_val) / abs(exp_val)
                    if drift > config.BALANCE_DRIFT_THRESHOLD:
                        logger.warning(f"DRIFT DETECTED in {field}/{key}:")
                        logger.warning(f"  Expected: {exp_val}, Actual: {act_val}, Drift: {drift*100:.1f}%")
                        return False
    return True


def log_error(error: Exception, context: str = "") -> None:
    """Log an error with full context."""
    logger.error(f"ERROR in {context}: {type(error).__name__}: {str(error)}")
    import traceback
    logger.error(traceback.format_exc())


def log_warning(message: str) -> None:
    """Log a warning."""
    logger.warning(f"WARNING: {message}")


def log_info(message: str) -> None:
    """Log info."""
    logger.info(message)


def log_debug(message: str) -> None:
    """Log debug."""
    logger.debug(message)


def generate_hourly_report() -> Dict:
    """Generate a summary report of the last hour."""
    # Read last hour of decisions
    decisions_file = log_dir / config.DECISIONS_FILE
    if not decisions_file.exists():
        return {"error": "No decisions logged yet"}

    one_hour_ago = datetime.now(timezone.utc).timestamp() - 3600

    decisions = []
    with open(decisions_file, "r") as f:
        for line in f:
            try:
                data = json.loads(line)
                # Parse timestamp
                ts_str = data.get("_timestamp", "")
                if ts_str:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                    if ts > one_hour_ago:
                        decisions.append(data)
            except:
                continue

    # Summarize
    no_trade_count = sum(1 for d in decisions if d.get("action") == "NO_TRADE")
    swing_count = sum(1 for d in decisions if d.get("action") == "SWING")
    scalp_count = sum(1 for d in decisions if d.get("action") == "SCALP")
    executions = [d for d in decisions if d.get("type") == "execution"]
    successful_executions = sum(1 for e in executions if e.get("success"))

    report = {
        "period": "last_hour",
        "total_cycles": len([d for d in decisions if "action" in d and d.get("type") != "execution"]),
        "no_trade": no_trade_count,
        "swing_decisions": swing_count,
        "scalp_decisions": scalp_count,
        "trades_executed": len(executions),
        "successful_trades": successful_executions,
    }

    logger.info("=" * 60)
    logger.info("HOURLY REPORT")
    logger.info("=" * 60)
    for k, v in report.items():
        logger.info(f"  {k}: {v}")
    logger.info("=" * 60)

    return report
