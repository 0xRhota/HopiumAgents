#!/usr/bin/env python3
"""
Momentum Limit Order Bot

Pure algo: detect 5m trend → place POST_ONLY limit behind price → TP/SL exit.

Usage:
    # Single asset
    python scripts/momentum_mm.py --exchange hibachi --asset BTC

    # Explicit multi-asset
    python scripts/momentum_mm.py --exchange nado --assets BTC,DOGE,WLFI,kBONK

    # Auto-discover ALL tradeable markets on exchange
    python scripts/momentum_mm.py --exchange hibachi --assets all
    python scripts/momentum_mm.py --exchange nado --assets all
    python scripts/momentum_mm.py --exchange extended --assets all

Markets are auto-discovered from exchange API. Binance TA data availability
is validated at startup — assets without Binance klines are skipped.
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

import requests
import pandas as pd

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.strategies.momentum.engine import MomentumEngine, MomentumConfig
from core.strategies.momentum.exchange_adapter import create_adapter, ExchangeAdapter
from core.strategies.momentum.self_learning import MomentumLearner

# ─── Logging ──────────────────────────────────────────────────────

LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "momentum"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging(exchange: str, asset: str = ""):
    """Configure logging: console + file."""
    suffix = f"_{asset.lower()}" if asset else ""
    log_file = LOG_DIR / f"{exchange}{suffix}_bot.log"

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%H:%M:%S',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )
    logging.getLogger('urllib3').setLevel(logging.ERROR)
    logging.getLogger('aiohttp').setLevel(logging.ERROR)
    logging.getLogger('asyncio').setLevel(logging.ERROR)


logger = logging.getLogger(__name__)


# ─── Data Fetching ────────────────────────────────────────────────

# ─── Dynamic Market Discovery ────────────────────────────────────
#
# EXCHANGE_SYMBOLS is populated at startup:
#   --assets all   → fetches from exchange API + validates Binance TA data
#   --assets X,Y   → builds only for requested assets
#   --asset X      → single asset (legacy)
#
# No need to hardcode every asset. The bot auto-discovers what's available.

# Kilo-token mappings: exchange asset → (binance_base, price_scale)
# Nado uses kBONK/kPEPE, Extended uses 1000BONK/1000PEPE/1000SHIB
KILO_TOKENS = {
    "kBONK": ("BONK", 1000),
    "kPEPE": ("PEPE", 1000),
    "1000BONK": ("BONK", 1000),
    "1000PEPE": ("PEPE", 1000),
    "1000SHIB": ("SHIB", 1000),
}

# Static fallback (only used for dry-run when no adapter available)
STATIC_EXCHANGE_SYMBOLS = {}

# These get populated dynamically at startup
EXCHANGE_SYMBOLS: dict = {}
BINANCE_SYMBOLS: dict = {}
BINANCE_PRICE_SCALE: dict = {}


def asset_to_binance(asset: str) -> tuple:
    """Derive Binance symbol and price scale from asset name.

    Returns (binance_symbol, price_scale) or (None, 1) if can't map.
    """
    if asset in KILO_TOKENS:
        base, scale = KILO_TOKENS[asset]
        return f"{base}USDT", scale
    return f"{asset}USDT", 1


def validate_binance_symbol(binance_sym: str) -> bool:
    """Quick check if Binance has data for this symbol."""
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": binance_sym, "interval": "5m", "limit": 1},
            timeout=5,
        )
        return resp.status_code == 200 and len(resp.json()) > 0
    except Exception:
        return False


def fetch_recent_candles(asset: str, count: int = 50) -> Optional[pd.DataFrame]:
    """Fetch recent 15m candles from Binance for trend detection."""
    binance_symbol = BINANCE_SYMBOLS.get(asset)
    if not binance_symbol:
        logger.error(f"Unknown asset: {asset}")
        return None

    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": binance_symbol, "interval": "15m", "limit": count},
            timeout=10,
        )
        data = resp.json()
        if not data:
            return None

        df = pd.DataFrame(data, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore",
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)

        # Scale prices for kilo-tokens (e.g., kBONK = 1000 * BONK)
        scale = BINANCE_PRICE_SCALE.get(asset, 1)
        if scale != 1:
            for col in ["open", "high", "low", "close"]:
                df[col] = df[col] * scale

        return df

    except Exception as e:
        logger.error(f"Binance klines fetch failed: {e}")
        return None


# ─── JSONL / CSV Logging ─────────────────────────────────────────

def append_jsonl(filepath: Path, data: dict):
    """Append a JSON line to a file."""
    data["_timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(filepath, "a") as f:
        f.write(json.dumps(data) + "\n")


def append_csv(filepath: Path, row: dict):
    """Append a row to a CSV file (creates header on first write)."""
    exists = filepath.exists() and filepath.stat().st_size > 0
    with open(filepath, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            writer.writeheader()
        writer.writerow(row)


# ─── LLM Monk Mode Filter ────────────────────────────────────────

def llm_veto(asset: str, direction: str, trend: dict) -> bool:
    """
    Optional LLM Monk Mode filter. LLM can veto trades but not force them.

    Returns True if trade should be VETOED (skipped).
    """
    api_key = os.getenv("OPEN_ROUTER")
    if not api_key:
        logger.warning("OPEN_ROUTER key not set, skipping LLM filter")
        return False

    prompt = (
        f"You are a conservative crypto trader using Monk Mode (trade sparingly).\n"
        f"Asset: {asset}\n"
        f"Proposed trade: {direction}\n"
        f"Trend strength: {trend['strength']:.2f}\n"
        f"ROC: {trend['roc_bps']:.1f} bps\n"
        f"RSI: {trend['rsi']}\n"
        f"Volume ratio: {trend['vol_ratio']:.1f}x\n\n"
        f"Should we take this trade? Reply ONLY with YES or NO and one sentence why."
    )

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "qwen/qwen3.6-plus:free",
                "messages": [
                    {"role": "system", "content": "You are a conservative trader. Only approve high-conviction setups."},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 100,
            },
            timeout=15,
        )
        answer = resp.json()["choices"][0]["message"]["content"].strip()
        logger.info(f"LLM Monk Mode: {answer}")

        # Veto if LLM says NO
        return answer.upper().startswith("NO")

    except Exception as e:
        logger.warning(f"LLM filter failed: {e}, allowing trade")
        return False


# ─── Main Bot ─────────────────────────────────────────────────────

class MomentumBot:
    """Main bot: wires engine + adapter + logging."""

    def __init__(
        self,
        exchange: str,
        asset: str,
        dry_run: bool = False,
        use_llm_filter: bool = False,
        interval: int = 60,
        adapter: Optional[ExchangeAdapter] = None,
    ):
        self.exchange = exchange
        self.asset = asset
        self.dry_run = dry_run
        self.use_llm_filter = use_llm_filter
        self.interval = interval

        # Exchange symbol
        symbols = EXCHANGE_SYMBOLS.get(exchange, {})
        self.symbol = symbols.get(asset)
        if not self.symbol:
            raise ValueError(f"Asset {asset} not supported on {exchange}")

        # Per-exchange config — backtest-optimized (Apr 9 2026)
        # Nado: 2×$100, TP80/SL40, score>=2.5, 1H trend filter
        # Hibachi: 3×$50, TP80/SL40, score>=2.5
        config = MomentumConfig()

        if exchange == "hibachi":
            config.offset_bps = 18.0     # No POST_ONLY — wider offset
            config.leverage = 5.0        # Hibachi 5x leverage
            config.max_positions = 3     # 3×$50 = $150/$250 = 60% margin
            config.size_pct = 33.0       # ~$50 per trade on $30 account at 5x
            config.tp_bps = 80.0         # 0.8% TP
            config.sl_bps = 40.0         # 0.4% SL
            config.max_hold_minutes = 120.0
            config.score_min = 2.5
            config.require_volume = False
        elif exchange == "nado":
            config.min_notional = 100.0  # Nado exchange minimum
            config.leverage = 10.0       # Nado 10x leverage
            config.max_positions = 2     # 2×$100 = $200/$500 = 40% margin
            config.size_pct = 20.0       # $100 per trade on $50 at 10x
            config.tp_bps = 80.0         # 0.8% TP
            config.sl_bps = 40.0         # 0.4% SL
            config.max_hold_minutes = 120.0
            config.score_min = 2.5
            config.require_volume = False
            config.offset_bps = 5.0      # Tighter offset for faster fills
        elif exchange == "extended":
            config.leverage = 10.0       # Extended 10x leverage

        self.engine = MomentumEngine(config)

        # Adapter: use shared if provided, else create own (skip for dry-run)
        self.adapter: Optional[ExchangeAdapter] = None
        if adapter:
            self.adapter = adapter
        elif not dry_run:
            self.adapter = create_adapter(exchange)

        # State
        self.position: Optional[dict] = None  # {side, entry_price, entry_time, size, order_id, equity_before}
        self.pending_order: Optional[dict] = None  # {order_id, side, price, size, placed_time}
        self._entry_blocked_until: float = 0.0  # Cooldown after order rejection
        self.cycle_count = 0
        self.total_trades = 0
        self.last_hourly_snapshot = 0.0
        self.managed_by_runner = adapter is not None  # Skip hourly snapshots if runner handles them
        self._sibling_bots: Optional[list] = None  # Set by multi-asset runner for position limits

        # Self-learning (shared per exchange — created once by multi-asset runner)
        self.learner: Optional[MomentumLearner] = None

        # Log files (include asset for multi-asset tracking)
        suffix = f"_{asset.lower()}"
        self.trades_file = LOG_DIR / f"{exchange}{suffix}_trades.jsonl"
        self.audit_file = LOG_DIR / f"{exchange}{suffix}_audit.csv"
        self.hourly_file = LOG_DIR / f"{exchange}{suffix}_hourly.jsonl"

    async def run(self):
        """Main loop."""
        tag = f"[{self.exchange.upper()}|{self.asset}]"
        mode = "DRY-RUN" if self.dry_run else "LIVE"

        logger.info("=" * 60)
        logger.info(f"{tag} Momentum Bot Starting ({mode})")
        logger.info(f"  Symbol: {self.symbol}")
        logger.info(f"  Offset: {self.engine.config.offset_bps} bps")
        logger.info(f"  Exit: signal-based (TREND_FLIP + {self.engine.config.emergency_sl_bps}bps emergency SL)")
        logger.info(f"  Score min: {self.engine.config.score_min}/5.0")
        logger.info(f"  Max positions: {self.engine.config.max_positions}")
        logger.info(f"  Size: {self.engine.config.size_pct}% of buying power ({self.engine.config.leverage}x leverage, min notional: ${self.engine.config.min_notional})")
        logger.info(f"  POST_ONLY: {self.adapter.supports_post_only if self.adapter else 'N/A'}")
        logger.info(f"  LLM filter: {self.use_llm_filter}")
        logger.info(f"  Interval: {self.interval}s")
        logger.info("=" * 60)

        if self.adapter:
            equity = await self.adapter.get_equity()
            logger.info(f"{tag} Starting equity: ${equity:.2f}")
            self._snapshot_equity(equity, "BOT_START")

        while True:
            try:
                await self._cycle()
            except KeyboardInterrupt:
                logger.info(f"{tag} Shutting down...")
                if self.adapter and self.position:
                    logger.info(f"{tag} Closing open position...")
                    await self.adapter.cancel_all(self.symbol)
                    await self.adapter.close_position(self.symbol)
                break
            except Exception as e:
                logger.error(f"{tag} Cycle error: {e}", exc_info=True)

            await asyncio.sleep(self.interval)

    async def _cycle(self):
        """Single trading cycle."""
        self.cycle_count += 1
        tag = f"[{self.exchange.upper()}|{self.asset}]"

        # 0. Reconcile: if we think we're flat but exchange has a position, adopt it
        if not self.position and not self.pending_order and self.adapter:
            try:
                ex_pos = await self.adapter.get_position(self.symbol)
                if ex_pos and ex_pos["size"] > 0:
                    self.position = {
                        "side": ex_pos["side"],
                        "entry_price": ex_pos.get("entry_price", 0.0),
                        "entry_time": time.time(),  # Don't know real entry time
                        "size": ex_pos["size"],
                        "equity_before": 0.0,
                        "trend": {"strength": 0, "roc_bps": 0, "scoring": "reconciled", "score": 0},
                    }
                    logger.info(
                        f"{tag} RECONCILED: found existing {ex_pos['side']} "
                        f"size={ex_pos['size']} on exchange"
                    )
            except Exception as e:
                logger.warning(f"{tag} Reconcile check failed: {e}")

        # 1. Fetch candles
        df = fetch_recent_candles(self.asset)
        if df is None or len(df) < 25:
            logger.warning(f"{tag} Insufficient candle data")
            return

        # 2. Detect trend (v9 5-signal scoring)
        trend = self.engine.detect_trend(df)

        if self.cycle_count % 5 == 1:  # Log every 5th cycle
            logger.info(
                f"{tag} Score={trend['score']}/5.0 [{trend['scoring']}] "
                f"→ {trend['direction']} RSI={trend['rsi']} vol={trend['vol_ratio']:.1f}x"
            )

        # 3. Hourly equity snapshot (skip if multi-asset runner handles this)
        if not self.managed_by_runner:
            now = time.time()
            if now - self.last_hourly_snapshot >= 3600 and self.adapter:
                equity = await self.adapter.get_equity()
                self._snapshot_equity(equity, "HOURLY")
                append_jsonl(self.hourly_file, {
                    "exchange": self.exchange,
                    "asset": self.asset,
                    "equity": equity,
                    "cycle": self.cycle_count,
                    "trades_total": self.total_trades,
                })
                self.last_hourly_snapshot = now

        # 4. If position open → check TP/SL/TIME
        if self.position:
            await self._manage_position(trend)
            return

        # 5. Check pending limit order fill
        if self.pending_order:
            await self._check_pending_fill()
            return

        # 6. No position → evaluate entry
        if trend["direction"] == "NONE":
            return

        if self.engine.in_cooldown():
            return

        # 6b. Position limit check — don't open if exchange already at max
        max_pos = self.engine.config.max_positions
        if max_pos > 0 and self._sibling_bots:
            open_count = sum(
                1 for b in self._sibling_bots
                if b.position is not None or b.pending_order is not None
            )
            if open_count >= max_pos:
                return  # At capacity, skip silently

        # 7. Self-learning gate
        if self.learner:
            allowed, reason = self.learner.should_trade(self.asset, trend["score"])
            if not allowed:
                if self.cycle_count % 10 == 1:
                    logger.info(f"{tag} SELF-LEARN BLOCKED: {reason}")
                return

        # 8. LLM Monk Mode veto
        if self.use_llm_filter:
            if llm_veto(self.asset, trend["direction"], trend):
                logger.info(f"{tag} LLM VETOED {trend['direction']}")
                return

        # 9. Place entry order
        await self._place_entry(trend)

    async def _place_entry(self, trend: dict):
        """Place a limit order behind current price. Size from account equity."""
        tag = f"[{self.exchange.upper()}|{self.asset}]"

        # Skip if this asset recently had an order rejected (min size, etc.)
        if time.time() < self._entry_blocked_until:
            return

        if self.dry_run:
            current_price = float(fetch_recent_candles(self.asset, 1).iloc[-1]["close"])
        else:
            current_price = await self.adapter.get_price(self.symbol)

        if not current_price:
            logger.warning(f"{tag} Cannot get price")
            return

        # Dynamic sizing: % of leveraged buying power
        equity_before = await self.adapter.get_equity() if self.adapter else 100.0
        cfg = self.engine.config
        buying_power = equity_before * cfg.leverage
        size_usd = buying_power * cfg.size_pct / 100.0

        # Respect exchange min notional (compare against buying power, not raw equity)
        if cfg.min_notional > 0 and size_usd < cfg.min_notional:
            if buying_power >= cfg.min_notional:
                size_usd = cfg.min_notional
            else:
                logger.info(f"{tag} Buying power ${buying_power:.2f} too low for min notional ${cfg.min_notional}")
                return

        direction = trend["direction"]
        entry_price = self.engine.calculate_entry(current_price, direction)
        size = size_usd / entry_price

        side = "BUY" if direction == "LONG" else "SELL"

        # Use enough decimals for sub-dollar assets
        pd = 6 if current_price < 1 else 2
        logger.info(
            f"{tag} ENTRY: {side} {self.asset} @ ${entry_price:.{pd}f} "
            f"(current ${current_price:.{pd}f}) "
            f"size=${size_usd:.2f} ({cfg.size_pct}% of ${buying_power:.2f} [{cfg.leverage}x lev]) "
            f"score={trend['score']}/5.0 [{trend['scoring']}]"
        )

        if self.dry_run:
            logger.info(f"{tag} [DRY-RUN] Would place {side} limit @ ${entry_price:,.2f}")
            return

        self._snapshot_equity(equity_before, "PRE_OPEN")

        order_id = await self.adapter.place_limit(
            self.symbol, side, entry_price, round(size, 6)
        )

        if order_id:
            self.pending_order = {
                "order_id": order_id,
                "side": direction,
                "price": entry_price,
                "size": round(size, 6),
                "size_usd": round(size_usd, 2),
                "placed_time": time.time(),
                "equity_before": equity_before,
                "trend": trend,
            }
            logger.info(f"{tag} Order placed: {order_id}")
        else:
            logger.warning(f"{tag} Order placement failed")
            # Cooldown this asset for 30 min to avoid spamming rejected orders
            self._entry_blocked_until = time.time() + 1800

    async def _check_pending_fill(self):
        """Check if pending limit order has filled."""
        tag = f"[{self.exchange.upper()}|{self.asset}]"

        if self.dry_run:
            return

        # Check if we now have a position
        pos = await self.adapter.get_position(self.symbol)

        if pos and pos["size"] > 0:
            # Order filled!
            logger.info(f"{tag} ORDER FILLED: {pos['side']} {pos['size']}")
            self.position = {
                "side": self.pending_order["side"],
                "entry_price": self.pending_order["price"],
                "entry_time": time.time(),
                "size": pos["size"],
                "size_usd": self.pending_order.get("size_usd", 0),
                "equity_before": self.pending_order["equity_before"],
                "trend": self.pending_order["trend"],
            }
            self.pending_order = None
            return

        # Check if order expired (5 min TTL)
        elapsed = time.time() - self.pending_order["placed_time"]
        if elapsed > 300:
            logger.info(f"{tag} Pending order expired, cancelling")
            await self.adapter.cancel_all(self.symbol)
            self.pending_order = None

    async def _manage_position(self, trend: dict):
        """Monitor open position — exit on trend flip or emergency SL."""
        tag = f"[{self.exchange.upper()}|{self.asset}]"

        if self.dry_run:
            return

        current_price = await self.adapter.get_price(self.symbol)
        if not current_price:
            return

        exit_reason = self.engine.should_exit(
            current_price,
            self.position["entry_price"],
            self.position["side"],
            self.position["entry_time"],
            trend,
        )

        if not exit_reason:
            return

        # Close position
        logger.info(
            f"{tag} EXIT ({exit_reason}): {self.position['side']} "
            f"entry=${self.position['entry_price']:,.2f} "
            f"exit=${current_price:,.2f}"
        )

        # Snapshot equity BEFORE close
        equity_pre_close = await self.adapter.get_equity()
        self._snapshot_equity(equity_pre_close, "PRE_CLOSE")

        success = await self.adapter.close_position(self.symbol)

        if success:
            # Wait briefly for settlement
            await asyncio.sleep(2)

            # Snapshot equity AFTER close
            equity_post_close = await self.adapter.get_equity()
            self._snapshot_equity(equity_post_close, "POST_CLOSE")

            # Calculated PnL from entry/exit prices (accurate per-position)
            entry_p = self.position["entry_price"]
            size = self.position["size"]
            reconciled = entry_p <= 0  # adopted from exchange at startup — no real entry price
            if reconciled:
                pnl_calc = None
            elif self.position["side"] == "LONG":
                pnl_calc = (current_price - entry_p) * size
            else:
                pnl_calc = (entry_p - current_price) * size

            # Balance-delta (secondary — inaccurate with shared accounts)
            pnl_balance_delta = equity_post_close - self.position["equity_before"]

            self.total_trades += 1
            hold_minutes = (time.time() - self.position["entry_time"]) / 60.0

            pnl_display = f"${pnl_calc:+.4f}" if pnl_calc is not None else "UNKNOWN (reconciled, entry_price=0)"
            logger.info(
                f"{tag} TRADE #{self.total_trades}: "
                f"PnL={pnl_display} "
                f"hold={hold_minutes:.1f}min reason={exit_reason}"
            )

            # Log trade to JSONL
            trade_record = {
                "id": str(uuid4())[:8],
                "exchange": self.exchange,
                "symbol": self.asset,
                "side": self.position["side"],
                "entry_price": self.position["entry_price"],
                "exit_price": current_price,
                "size": size,
                "size_usd": self.position.get("size_usd", size * self.position["entry_price"]),
                "pnl": round(pnl_calc, 6) if pnl_calc is not None else None,
                "pnl_balance_delta": round(pnl_balance_delta, 6),
                "reconciled": reconciled,
                "hold_minutes": round(hold_minutes, 1),
                "exit_reason": exit_reason,
                "score": self.position["trend"].get("score", 0),
                "scoring": self.position["trend"].get("scoring", ""),
                "trend_strength": self.position["trend"]["strength"],
                "roc_bps": self.position["trend"]["roc_bps"],
                "llm_filtered": self.use_llm_filter,
                "trade_number": self.total_trades,
            }
            append_jsonl(self.trades_file, trade_record)

            # Feed self-learning only with real pnl (skip reconciled — entry price unknown)
            if self.learner and pnl_calc is not None:
                self.learner.record_trade(
                    self.asset,
                    self.position["trend"].get("score", 0),
                    pnl_calc,
                )

            # Record cooldown
            self.engine.record_close()
        else:
            logger.error(f"{tag} Failed to close position! Will retry next cycle.")
            return  # Keep self.position so we retry next cycle

        self.position = None

    def _snapshot_equity(self, equity: float, event: str):
        """Log equity snapshot to audit CSV."""
        append_csv(self.audit_file, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "equity": round(equity, 6),
            "event": event,
            "cycle": self.cycle_count,
            "exchange": self.exchange,
            "asset": self.asset,
        })


# ─── Multi-Asset Runner ──────────────────────────────────────────


async def run_multi(
    exchange: str,
    assets: List[str],
    adapter: ExchangeAdapter,
    dry_run: bool = False,
    use_llm_filter: bool = False,
    interval: int = 60,
):
    """Run multiple assets on one exchange in a single process."""
    # Shared self-learning instance for this exchange
    learner = MomentumLearner(exchange, LOG_DIR)

    bots = []
    for asset in assets:
        bot = MomentumBot(
            exchange=exchange,
            asset=asset,
            dry_run=dry_run,
            use_llm_filter=use_llm_filter,
            interval=interval,
            adapter=adapter,
        )
        bot.learner = learner
        bots.append(bot)

    # Wire up sibling references for position limit checks
    for bot in bots:
        bot._sibling_bots = bots

    # Startup
    asset_list = ", ".join(assets)
    cfg = bots[0].engine.config
    logger.info("=" * 60)
    logger.info(f"[{exchange.upper()}] Momentum Bot v10 Starting (backtest-optimized)")
    logger.info(f"  Assets: {asset_list}")
    logger.info(f"  Score threshold: {cfg.score_min}/5.0")
    logger.info(f"  Max positions: {cfg.max_positions}")
    logger.info(f"  Exit: TP={cfg.tp_bps}bps SL={cfg.sl_bps}bps TIME={cfg.max_hold_minutes}min TREND_FLIP EMERGENCY_SL={cfg.emergency_sl_bps}bps")
    logger.info(f"  Size: {cfg.size_pct}% of buying power ({cfg.leverage}x leverage, min notional: ${cfg.min_notional})")
    logger.info(f"  Volume gate: {'ON' if cfg.require_volume else 'OFF'}")
    logger.info(f"  Self-learning: ACTIVE (circuit breaker + score bucket)")
    logger.info(f"  Interval: {interval}s")
    logger.info("=" * 60)

    if adapter:
        equity = await adapter.get_equity()
        logger.info(f"[{exchange.upper()}] Starting equity: ${equity:.2f}")

    # Shared hourly snapshot state
    hourly_file = LOG_DIR / f"{exchange}_hourly.jsonl"
    audit_file = LOG_DIR / f"{exchange}_audit.csv"
    last_hourly = 0.0

    while True:
        # Tick each asset bot sequentially
        for bot in bots:
            try:
                await bot._cycle()
            except Exception as e:
                logger.error(f"[{exchange.upper()}|{bot.asset}] Cycle error: {e}", exc_info=True)

        # Account-level hourly snapshot (once for all assets)
        now = time.time()
        if now - last_hourly >= 3600 and adapter:
            equity = await adapter.get_equity()
            # Skip snapshot if equity is clearly wrong (API failure returns -1 or 0)
            if equity > 0:
                append_csv(audit_file, {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "equity": round(equity, 6),
                    "event": "HOURLY",
                    "cycle": bots[0].cycle_count,
                    "exchange": exchange,
                    "asset": "ALL",
                })
                append_jsonl(hourly_file, {
                    "exchange": exchange,
                    "assets": asset_list,
                    "equity": equity,
                    "positions": sum(1 for b in bots if b.position is not None),
                    "pending": sum(1 for b in bots if b.pending_order is not None),
                    "total_trades": sum(b.total_trades for b in bots),
                })
                logger.info(
                    f"[{exchange.upper()}] HOURLY: equity=${equity:.2f} "
                    f"positions={sum(1 for b in bots if b.position)} "
                    f"trades={sum(b.total_trades for b in bots)}"
                )
            else:
                logger.warning(f"[{exchange.upper()}] HOURLY: skipped — equity API returned ${equity:.2f} (likely API error)")
            last_hourly = now

        await asyncio.sleep(interval)


# ─── CLI ──────────────────────────────────────────────────────────


def resolve_asset(raw: str, exchange: str) -> str:
    """Case-insensitive asset resolution (handles kBONK etc.)."""
    exchange_syms = EXCHANGE_SYMBOLS.get(exchange, {})
    for key in exchange_syms:
        if key.upper() == raw.upper():
            return key
    return raw.upper()


async def discover_and_register(adapter, exchange: str) -> List[str]:
    """Discover all markets on exchange, validate Binance TA, register symbols.

    Returns list of tradeable asset names.
    """
    markets = await adapter.discover_markets()
    if not markets:
        logger.warning(f"[{exchange}] No markets discovered, check adapter")
        return []

    valid_assets = []
    for m in markets:
        asset = m["asset"]
        symbol = m["symbol"]

        # Derive Binance symbol for TA data
        binance_sym, scale = asset_to_binance(asset)

        # Validate Binance has this symbol
        if not validate_binance_symbol(binance_sym):
            logger.info(f"  {asset} ({symbol}) — skipped (no Binance data for {binance_sym})")
            continue

        # Register in global mappings
        if exchange not in EXCHANGE_SYMBOLS:
            EXCHANGE_SYMBOLS[exchange] = {}
        EXCHANGE_SYMBOLS[exchange][asset] = symbol
        BINANCE_SYMBOLS[asset] = binance_sym
        if scale != 1:
            BINANCE_PRICE_SCALE[asset] = scale

        valid_assets.append(asset)
        logger.info(f"  {asset} ({symbol}) — OK (binance={binance_sym}, scale={scale}x)")

    return valid_assets


async def _async_main(args):
    """Async entry point — handles discovery before creating bots."""
    exchange = args.exchange

    if args.assets:
        setup_logging(exchange)  # Single log file for all assets
        adapter = None if args.dry_run else create_adapter(exchange)

        if args.assets.lower() == "all":
            # Auto-discover all markets from exchange API
            if not adapter:
                logger.error("Cannot discover markets in dry-run mode without adapter")
                return
            logger.info(f"[{exchange.upper()}] Discovering available markets...")
            assets = await discover_and_register(adapter, exchange)
            if not assets:
                logger.error(f"[{exchange.upper()}] No tradeable markets found")
                return
            logger.info(f"[{exchange.upper()}] Trading {len(assets)} assets: {', '.join(assets)}")
        else:
            # Explicit asset list — register them
            raw_assets = [a.strip() for a in args.assets.split(",") if a.strip()]
            if adapter:
                # Discover from exchange to get proper symbol mappings
                logger.info(f"[{exchange.upper()}] Discovering markets for validation...")
                all_markets = await discover_and_register(adapter, exchange)
                # Resolve requested assets against discovered
                assets = []
                for raw in raw_assets:
                    resolved = resolve_asset(raw, exchange)
                    if resolved in EXCHANGE_SYMBOLS.get(exchange, {}):
                        assets.append(resolved)
                    else:
                        logger.warning(f"  {raw} — not found on {exchange}, skipping")
                if not assets:
                    logger.error("No valid assets after validation")
                    return
            else:
                # Dry-run: use static fallback
                _register_static(exchange)
                assets = [resolve_asset(a, exchange) for a in raw_assets]

        await run_multi(
            exchange=exchange,
            assets=assets,
            adapter=adapter,
            dry_run=args.dry_run,
            use_llm_filter=args.llm_filter,
            interval=args.interval,
        )
    else:
        # Single-asset mode (backwards compatible)
        asset_raw = args.asset
        adapter = None if args.dry_run else create_adapter(exchange)

        if adapter:
            # Discover to get proper symbol mapping
            await discover_and_register(adapter, exchange)

        asset_resolved = resolve_asset(asset_raw, exchange)

        # Fallback: if discovery didn't find it, try static
        if asset_resolved not in EXCHANGE_SYMBOLS.get(exchange, {}):
            _register_static(exchange)
            asset_resolved = resolve_asset(asset_raw, exchange)

        setup_logging(exchange, asset_resolved)

        bot = MomentumBot(
            exchange=exchange,
            asset=asset_resolved,
            dry_run=args.dry_run,
            use_llm_filter=args.llm_filter,
            interval=args.interval,
        )

        await bot.run()


def _register_static(exchange: str):
    """Register static symbol mappings as fallback (for dry-run or when discovery fails)."""
    static = STATIC_EXCHANGE_SYMBOLS.get(exchange, {})
    if exchange not in EXCHANGE_SYMBOLS:
        EXCHANGE_SYMBOLS[exchange] = {}
    EXCHANGE_SYMBOLS[exchange].update(static)
    for asset in static:
        if asset not in BINANCE_SYMBOLS:
            bsym, scale = asset_to_binance(asset)
            BINANCE_SYMBOLS[asset] = bsym
            if scale != 1:
                BINANCE_PRICE_SCALE[asset] = scale


def main():
    parser = argparse.ArgumentParser(description="Momentum Limit Order Bot")
    parser.add_argument("--exchange", required=True, choices=["hibachi", "nado", "extended"])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--asset", help="Single asset (BTC, ETH, SOL)")
    group.add_argument("--assets", help="Comma-separated assets or 'all' for auto-discovery")
    parser.add_argument("--dry-run", action="store_true", help="Paper trade (no real orders)")
    parser.add_argument("--llm-filter", action="store_true", help="Enable LLM Monk Mode veto")
    parser.add_argument("--interval", type=int, default=60, help="Loop interval in seconds")
    args = parser.parse_args()

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
