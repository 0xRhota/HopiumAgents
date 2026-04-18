#!/usr/bin/env python3
"""
Grid Market Maker v18 - Nado DEX
Qwen-calibrated dynamic spread based on ROC volatility

Strategy: Place limit orders on both sides of mid price
- Grid resets on 0.5% price move
- Inventory skew at 70% threshold
- ROC-based trend pause with longer pauses
- DYNAMIC SPREAD: Automatically adjusts based on volatility (Qwen-calibrated)
- TIME-BASED REFRESH: Refresh orders every 5 minutes (US-001)
- STALE ORDER DETECTION: Refresh if orders drift >0.2% from mid (US-003)

v18 Changes (Qwen-calibrated spreads):
- Removed tight_spread_mode (redundant with proper dynamic bands)
- New spread bands calibrated between v12 (too tight) and v13 (too wide):
  - v12 used 1.5 bps calm → adverse selection losses (-8.5 bps avg, -$23.57/7d)
  - v13 used 15 bps calm → zero fills in 5+ hours
  - v18 uses 4 bps calm → middle ground per Qwen recommendation
- 6 tiers: 4/6/8/12/15/PAUSE bps

v18 Parameters (Dynamic Spread - Qwen calibrated):
- Spread: DYNAMIC based on ROC
  - ROC 0-5 bps → 4 bps spread (calm market)
  - ROC 5-10 bps → 6 bps spread (low volatility)
  - ROC 10-20 bps → 8 bps spread (moderate volatility)
  - ROC 20-30 bps → 12 bps spread (high volatility)
  - ROC 30-50 bps → 15 bps spread (very high volatility)
  - ROC >50 bps → PAUSE orders (trend detected)
- ROC threshold: 50 bps for pause
- Pause duration: 5 minutes
- Grid reset: 0.5% price move OR 5 min time-based refresh
"""

import os
import sys
import time
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from collections import deque

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# LLM Trading - use simple API call
import aiohttp

# Load env
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ[key] = val.strip('"').strip("'")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

from dexes.nado.nado_sdk import NadoSDK


class GridMarketMakerNado:
    """
    Grid Market Maker v12 for Nado - Dynamic spread based on ROC volatility
    """

    def __init__(
        self,
        symbol: str = "ETH-PERP",
        base_spread_bps: float = 8.0,      # v11: 8 bps (15 was too wide) per Qwen
        order_size_usd: float = 100.0,     # $100 per order
        num_levels: int = 2,               # 2 levels per side
        max_inventory_pct: float = 350.0,  # v19: Allow 3.5x leverage (needed for $100 min orders with $40 capital)
        capital: float = 90.0,             # Account capital
        hedge_symbol: str = "BTC-PERP",    # Cross-asset LONG hedge
        hedge_size_pct: float = 80.0,      # Use 80% of capital for hedge (increased for tariff play)
        roc_threshold_bps: float = 50.0,   # v10: 50 bps ROC threshold (was 3) per Qwen
        min_pause_duration: int = 300,     # v10: 5 min pause (was 15s) per Qwen
    ):
        self.symbol = symbol
        self.base_spread_bps = base_spread_bps
        self.order_size_usd = order_size_usd
        self.num_levels = num_levels
        self.max_inventory_pct = max_inventory_pct
        self.capital = capital
        self.hedge_symbol = hedge_symbol
        self.hedge_size_pct = hedge_size_pct
        self.roc_threshold_bps = roc_threshold_bps
        self.min_pause_duration = min_pause_duration

        # Hedge position tracking
        self.hedge_position_opened = False

        # State
        self.sdk: Optional[NadoSDK] = None
        self.grid_center = None
        self.open_orders: Dict[str, Dict] = {}  # digest -> order info
        self.price_history: deque = deque(maxlen=360)  # 6 minutes at 1/sec

        # Trend detection
        self.orders_paused = False
        self.pause_side = None
        self.pause_start_time = None

        # Dynamic spread tracking
        self.current_spread_bps = base_spread_bps
        self.last_spread_bps = base_spread_bps

        # Stats
        self.total_volume = 0.0
        self.fills_count = 0
        self.start_time = None
        self.initial_balance = 0.0

        # NP-004: Fill rate monitoring
        self.last_fill_time = None
        self.no_fill_alert_threshold_seconds = 1800  # Alert if 0 fills for 30 minutes
        self.no_fill_alert_triggered = False

        # v20: Real fill tracking via Archive API (replaces broken vanished-order detection)
        self.last_submission_idx = None  # Track last seen match idx

        # v14: Cooldown after placing orders (skip fill check for N cycles)
        self.skip_fill_check_cycles = 0

        # v15: Time-based refresh (US-001)
        self.last_refresh_time = None
        self.time_refresh_interval = 300  # 5 minutes

        # v18: Tight spread mode removed - dynamic bands handle it directly

        # v17: Inventory rebalancing (NP-003)
        self.max_inventory_start_time = None
        self.last_rebalance_time = None
        self.rebalance_threshold_pct = 55.0  # Trigger at 55% of max_inventory (~96% of capital with 175% leverage)
        self.rebalance_wait_seconds = 900  # 15 minutes at max inventory
        self.rebalance_cooldown_seconds = 3600  # Once per hour max
        self.rebalance_close_pct = 0.25  # Close 25% of position
        self.fills_since_max_inventory = 0

        # Position tracking
        self.position_size = 0.0
        self.position_notional = 0.0

        # Market info
        self.product_id = None
        self.tick_size = 0.1
        self.step_size = 0.001
        self.min_notional = 100.0  # Nado min is $100

        # Grid reset threshold (v10: 0.5% price move, was 0.25%) per Qwen
        self.grid_reset_pct = 0.50

        # ═══════════════════════════════════════════════════════════════════
        # LLM TRADING: Open directional longs on BTC/SOL when market looks good
        # ═══════════════════════════════════════════════════════════════════
        self.llm_enabled = True
        self.llm_check_interval = 600  # Check every 10 minutes
        self.llm_last_check = None
        self.llm_position_size_usd = 10.0  # $10 per LLM position (reduced for low capital)
        self.llm_max_positions = 2  # Max 2 LLM positions at a time
        self.llm_symbols = ["BTC-PERP", "SOL-PERP"]  # Assets to consider

        # LLM position tracking: {symbol: {entry_price, entry_time, size, side}}
        self.llm_positions: Dict[str, Dict] = {}

        # LLM exit rules
        self.llm_profit_target_pct = 2.0   # Take profit at +2%
        self.llm_stop_loss_pct = 1.5       # Stop loss at -1.5%
        self.llm_max_hold_hours = 4        # Max hold time

        # LLM API key (from env)
        self.llm_api_key = os.getenv('OPEN_ROUTER')

    async def initialize(self):
        """Initialize Nado SDK"""
        logger.info("=" * 70)
        logger.info("NADO GRID MM v18 - QWEN-CALIBRATED DYNAMIC SPREAD")
        logger.info("=" * 70)
        logger.info(f"Symbol: {self.symbol}")
        logger.info(f"Spread: DYNAMIC (4-15 bps, Qwen-calibrated)")
        logger.info(f"  ROC 0-5 bps → 4 bps spread (calm)")
        logger.info(f"  ROC 5-10 bps → 6 bps spread (low vol)")
        logger.info(f"  ROC 10-20 bps → 8 bps spread (moderate)")
        logger.info(f"  ROC 20-30 bps → 12 bps spread (high vol)")
        logger.info(f"  ROC 30-50 bps → 15 bps spread (very high vol)")
        logger.info(f"  ROC >50 bps → PAUSE orders (trend detected)")
        logger.info(f"ROC Window: 1 minute (fast reaction)")
        logger.info(f"Order Size: ${self.order_size_usd}")
        logger.info(f"Levels: {self.num_levels} per side")
        logger.info(f"Grid Reset: {self.grid_reset_pct}% price move")
        logger.info(f"ROC Threshold: {self.roc_threshold_bps} bps")
        logger.info(f"Min Pause: {self.min_pause_duration}s")
        logger.info(f"Time-based Refresh: {self.time_refresh_interval}s")
        logger.info("=" * 70)

        # Initialize SDK
        wallet_address = os.getenv('NADO_WALLET_ADDRESS')
        signer_key = os.getenv('NADO_LINKED_SIGNER_PRIVATE_KEY')
        subaccount_name = os.getenv('NADO_SUBACCOUNT_NAME', 'default')

        if not wallet_address or not signer_key:
            raise ValueError("NADO_WALLET_ADDRESS and NADO_LINKED_SIGNER_PRIVATE_KEY required in .env")

        self.sdk = NadoSDK(wallet_address, signer_key, subaccount_name=subaccount_name)

        # Verify linked signer
        if not await self.sdk.verify_linked_signer():
            raise ValueError("Linked signer not verified")
        logger.info("Linked signer verified!")

        # Get balance
        balance = await self.sdk.get_balance()
        self.initial_balance = balance or 0
        # Use actual balance as capital (dynamic, not hardcoded)
        self.capital = self.initial_balance
        logger.info(f"Account balance: ${self.initial_balance:.2f}")

        # Get product info
        product = await self.sdk.get_product_by_symbol(self.symbol)
        if not product:
            raise ValueError(f"Product {self.symbol} not found")

        self.product_id = product.get('product_id')
        self.tick_size = float(product.get('quote_currency_price_increment', 0.1))
        self.step_size = float(product.get('base_currency_increment', 0.001))
        self.min_notional = 100.0  # Nado min
        logger.info(f"Product ID: {self.product_id}, tick={self.tick_size}, step={self.step_size}, min=${self.min_notional}")

        # Get initial price
        mid = await self._get_mid_price()
        if not mid:
            raise Exception("Cannot get initial price")

        logger.info(f"Initial price: ${mid:,.2f}")
        self.grid_center = mid
        self.price_history.append(mid)
        self.start_time = datetime.now()

        # Sync position
        await self._sync_position()

        # DISABLED: Hedge feature removed per user request (2026-01-15)
        # await self._open_hedge_long()

        # Initialize LLM trading components
        if self.llm_enabled:
            await self._initialize_llm()

        return True

    async def _get_mid_price(self) -> Optional[float]:
        """Get current mid price from Nado market_price query"""
        try:
            response = await self.sdk._query("market_price", {"product_id": str(self.product_id)})
            if response.get("status") == "success":
                data = response.get("data", {})
                bid = self.sdk._from_x18(int(data.get('bid_x18', '0')))
                ask = self.sdk._from_x18(int(data.get('ask_x18', '0')))
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2
        except Exception as e:
            logger.error(f"Mid price error: {e}")
        return None

    async def _sync_position(self):
        """Sync position from exchange"""
        try:
            self.position_size = await self.sdk.get_position_size(self.symbol)
            mid = await self._get_mid_price()
            if mid:
                self.position_notional = self.position_size * mid
                if self.position_size != 0:
                    side = 'LONG' if self.position_size > 0 else 'SHORT'
                    logger.info(f"Position: {side} {abs(self.position_size):.6f} (${abs(self.position_notional):,.2f})")
        except Exception as e:
            logger.error(f"Position sync error: {e}")

    async def _open_hedge_long(self):
        """Open a LONG position on the hedge asset (BTC when grid is ETH)"""
        if self.hedge_position_opened:
            return

        try:
            # Check current hedge position
            hedge_size = await self.sdk.get_position_size(self.hedge_symbol)

            # Get hedge asset price first (needed for both check and order)
            hedge_product = await self.sdk.get_product_by_symbol(self.hedge_symbol)
            if not hedge_product:
                logger.error(f"Hedge product {self.hedge_symbol} not found")
                return

            hedge_product_id = hedge_product.get('product_id')
            response = await self.sdk._query("market_price", {"product_id": str(hedge_product_id)})
            if response.get("status") != "success":
                logger.error("Cannot get hedge price")
                return

            data = response.get("data", {})
            bid = self.sdk._from_x18(int(data.get('bid_x18', '0')))
            ask = self.sdk._from_x18(int(data.get('ask_x18', '0')))
            hedge_price = (bid + ask) / 2

            # Calculate target hedge size
            target_notional = self.capital * (self.hedge_size_pct / 100)
            current_notional = hedge_size * hedge_price if hedge_size > 0 else 0

            if hedge_size > 0:
                logger.info(f"Current hedge: {self.hedge_symbol} = {hedge_size:.6f} (${current_notional:.2f})")
                # Check if we need to top up
                if current_notional >= target_notional * 0.9:  # Within 10% of target
                    logger.info(f"✅ Hedge at target: ${current_notional:.2f} / ${target_notional:.2f}")
                    self.hedge_position_opened = True
                    return
                else:
                    # Need to add more
                    additional_notional = target_notional - current_notional
                    logger.info(f"🔄 Topping up hedge: need ${additional_notional:.2f} more")

            # If SHORT, close it first
            if hedge_size < 0:
                logger.info(f"🔄 Closing SHORT hedge on {self.hedge_symbol}...")
                result = await self.sdk.create_market_order(
                    symbol=self.hedge_symbol,
                    is_buy=True,
                    amount=abs(hedge_size)
                )
                if result and result.get('status') == 'success':
                    logger.info(f"  ✅ Closed SHORT {self.hedge_symbol}")
                await asyncio.sleep(1)
                current_notional = 0  # Reset after closing short

            # Calculate amount to add (full target if new, difference if topping up)
            hedge_notional = target_notional - current_notional

            if hedge_notional < 5:  # Min $5 order
                logger.info(f"Hedge amount too small: ${hedge_notional:.2f}")
                self.hedge_position_opened = True
                return

            # Calculate size
            import math
            step_size_raw = hedge_product.get('size_increment') or hedge_product.get('base_increment')
            step_size = self.sdk._from_x18(int(step_size_raw)) if step_size_raw else 0.0001
            hedge_amount = hedge_notional / hedge_price
            hedge_amount = math.ceil(hedge_amount / step_size) * step_size

            action = "Topping up" if current_notional > 0 else "Opening"
            logger.info(f"🚀 {action} LONG hedge: {self.hedge_symbol} ${hedge_notional:.2f} @ ${hedge_price:,.2f}")

            result = await self.sdk.create_market_order(
                symbol=self.hedge_symbol,
                is_buy=True,
                amount=hedge_amount
            )

            if result and result.get('status') == 'success':
                final_notional = current_notional + hedge_notional
                logger.info(f"  ✅ HEDGE LONG: {self.hedge_symbol} +{hedge_amount:.6f} (total ${final_notional:.2f})")
                self.hedge_position_opened = True
            else:
                logger.error(f"  ❌ Hedge order failed: {result}")

        except Exception as e:
            logger.error(f"Hedge open error: {e}")

    def _round_price(self, price: float) -> float:
        """Round price to tick size (avoid floating point errors)"""
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, 2)  # Round to 2 decimals to avoid FP errors

    def _round_size(self, size: float) -> float:
        """Round size to step size - round UP to ensure min notional is met"""
        import math
        steps = math.ceil(size / self.step_size)  # Round UP not down
        result = max(steps * self.step_size, self.step_size)
        return round(result, 3)  # Round to 3 decimals to avoid FP errors like 0.043000000000003

    def _calculate_roc(self) -> float:
        """Calculate Rate of Change in bps over 3-minute window (v14: proper window per LEARNINGS.md)"""
        if len(self.price_history) < 180:
            return 0.0  # Need 3 min of data before calculating ROC
        prices = list(self.price_history)
        current = prices[-1]
        past = prices[-180]  # 3 minutes ago (180 samples at 1/sec) - per LEARNINGS.md ROC window fix
        if past == 0:
            return 0.0
        return (current - past) / past * 10000

    def _calculate_dynamic_spread(self, roc: float) -> float:
        """
        Calculate dynamic spread based on ROC (volatility).

        v19 (aggressive fills): v18 at 4 bps still produced ZERO fills overnight.
        Tightening aggressively to get volume while keeping dynamic protection.

        Spread bands (v19 - aggressive):
        | ROC (abs) | Spread | Rationale |
        |-----------|--------|-----------|
        | 0-5 bps   | 1.5 bps| Calm - match v12 tightness for fills |
        | 5-10 bps  | 2.5 bps| Low vol - still tight |
        | 10-20 bps | 4 bps  | Moderate vol - was v18 calm level |
        | 20-30 bps | 6 bps  | High vol - protect |
        | 30-50 bps | 8 bps  | Very high vol |
        | > 50 bps  | PAUSE  | Stop trading, trend detected |
        """
        abs_roc = abs(roc)

        if abs_roc < 5:
            spread = 1.5
        elif abs_roc < 10:
            spread = 2.5
        elif abs_roc < 20:
            spread = 4.0
        elif abs_roc < 30:
            spread = 6.0
        elif abs_roc < 50:
            spread = 8.0
        else:
            spread = 0.0  # Will trigger pause logic

        return spread


    async def _check_inventory_rebalance(self, inventory_pct: float, fills: int) -> bool:
        """
        NP-003: Check if inventory rebalance is needed.

        - Track time at max inventory (>=95%)
        - After 15 minutes at max inventory with 0 fills, close 25% of position
        - Limit to once per hour maximum

        Args:
            inventory_pct: Current inventory as percentage of max (0-100+)
            fills: Number of fills in this cycle

        Returns:
            True if rebalance was executed, False otherwise
        """
        # Track fills while at max inventory
        if fills > 0:
            self.fills_since_max_inventory += fills

        # Check if at max inventory
        at_max_inventory = inventory_pct >= self.rebalance_threshold_pct

        # Debug: Log inventory check every call when high inventory
        if inventory_pct > 90:
            time_at_max = (datetime.now() - self.max_inventory_start_time).total_seconds() if self.max_inventory_start_time else 0
            logger.info(f"  [REBAL-CHECK] inv={inventory_pct:.0f}% >=95={at_max_inventory} timer={time_at_max:.0f}s fills={self.fills_since_max_inventory}")

        if at_max_inventory:
            # Start tracking if not already
            if self.max_inventory_start_time is None:
                self.max_inventory_start_time = datetime.now()
                self.fills_since_max_inventory = 0
                logger.info(f"  ⚠️ At {inventory_pct:.0f}% inventory - starting rebalance timer")
                return False

            # Check how long at max inventory
            time_at_max = (datetime.now() - self.max_inventory_start_time).total_seconds()

            # Check cooldown from last rebalance
            if self.last_rebalance_time:
                time_since_rebalance = (datetime.now() - self.last_rebalance_time).total_seconds()
                if time_since_rebalance < self.rebalance_cooldown_seconds:
                    return False  # Still in cooldown

            # Check if should rebalance: 15 min at max inventory with 0 fills
            if time_at_max >= self.rebalance_wait_seconds and self.fills_since_max_inventory == 0:
                # Execute rebalance
                await self._execute_inventory_rebalance()
                return True

        else:
            # Not at max inventory - reset tracking
            if self.max_inventory_start_time is not None:
                logger.info(f"  ✅ Inventory reduced to {inventory_pct:.0f}% - rebalance timer reset")
            self.max_inventory_start_time = None
            self.fills_since_max_inventory = 0

        return False

    async def _execute_inventory_rebalance(self):
        """
        NP-003: Execute inventory rebalance by closing 25% of position via market order.
        """
        if self.position_size == 0:
            return

        close_size = abs(self.position_size) * self.rebalance_close_pct
        close_notional = close_size * (await self._get_mid_price() or 0)

        # Determine direction - close means opposite of position
        is_buy = self.position_size < 0  # If SHORT, buy to close

        side_str = "LONG" if self.position_size > 0 else "SHORT"
        action_str = "SELL" if self.position_size > 0 else "BUY"

        logger.warning(f"  🔄 Inventory rebalance: closing 25% of {side_str} position")
        logger.warning(f"     {action_str} {close_size:.6f} (~${close_notional:.2f})")

        try:
            result = await self.sdk.create_market_order(
                symbol=self.symbol,
                is_buy=is_buy,
                amount=self._round_size(close_size)
            )

            if result and result.get('status') == 'success':
                logger.info(f"  ✅ Rebalance executed successfully")
                self.last_rebalance_time = datetime.now()
                self.max_inventory_start_time = None
                self.fills_since_max_inventory = 0
                # Sync position after rebalance
                await self._sync_position()
            else:
                logger.error(f"  ❌ Rebalance failed: {result}")

        except Exception as e:
            logger.error(f"  ❌ Rebalance error: {e}")

    def _check_stale_orders(self, mid_price: float) -> Optional[tuple]:
        """
        Check if any tracked orders are stale (>0.2% from mid price).

        US-003: Stale order detection
        - Calculate distance of tracked orders from current mid price
        - If any order is >0.2% from mid, return (order_price, distance_pct)
        - Returns None if no stale orders

        Returns:
            Optional[tuple]: (stale_order_price, distance_pct) if stale order found, else None
        """
        if not self.open_orders or not mid_price:
            return None

        stale_threshold_pct = 0.2  # 0.2% = 20 bps

        for digest, order_info in self.open_orders.items():
            order_price = order_info.get('price', 0)
            if order_price <= 0:
                continue

            distance_pct = abs(order_price - mid_price) / mid_price * 100

            if distance_pct > stale_threshold_pct:
                return (order_price, distance_pct)

        return None

    def _update_pause_state(self, roc: float):
        """Update order pause state based on trend - EXACT v8 logic"""
        old_paused = self.orders_paused
        old_side = self.pause_side

        strong_trend_threshold = self.roc_threshold_bps * 2.0

        if abs(roc) > strong_trend_threshold:
            if not self.orders_paused or self.pause_side != 'ALL':
                self.pause_start_time = datetime.now()
            self.orders_paused = True
            self.pause_side = 'ALL'
            direction = "UP" if roc > 0 else "DOWN"
            if not old_paused or old_side != 'ALL':
                logger.warning(f"  STRONG TREND {direction} - PAUSE ALL (ROC: {roc:+.2f} bps)")
        elif roc > self.roc_threshold_bps:
            if not self.orders_paused or self.pause_side != 'SELL':
                self.pause_start_time = datetime.now()
            self.orders_paused = True
            self.pause_side = 'SELL'
        elif roc < -self.roc_threshold_bps:
            if not self.orders_paused or self.pause_side != 'BUY':
                self.pause_start_time = datetime.now()
            self.orders_paused = True
            self.pause_side = 'BUY'
        else:
            if self.orders_paused and self.pause_start_time:
                elapsed = (datetime.now() - self.pause_start_time).total_seconds()
                if elapsed >= self.min_pause_duration:
                    self.orders_paused = False
                    self.pause_side = None
                    self.pause_start_time = None
            else:
                self.orders_paused = False
                self.pause_side = None

        if self.orders_paused and not old_paused and self.pause_side != 'ALL':
            logger.info(f"  PAUSE {self.pause_side} orders (ROC: {roc:+.2f} bps)")
        elif not self.orders_paused and old_paused:
            logger.info(f"  RESUME orders (ROC: {roc:+.2f} bps)")

    async def _cancel_all_orders(self):
        """Cancel all open orders"""
        try:
            result = await self.sdk.cancel_all_orders(self.symbol)
            if result:
                logger.info(f"  Orders cancelled")
            self.open_orders.clear()
        except Exception as e:
            logger.error(f"Cancel error: {e}")
            self.open_orders.clear()

    async def _place_grid_orders(self, mid_price: float, roc: float = 0.0):
        """Place grid orders with dynamic spread based on volatility (ROC)"""
        # First cancel existing orders
        await self._cancel_all_orders()

        # If ALL orders paused, don't place anything
        if self.orders_paused and self.pause_side == 'ALL':
            logger.info(f"  Grid SKIPPED: Strong trend, all orders paused")
            return

        # Calculate dynamic spread based on current volatility
        self.current_spread_bps = self._calculate_dynamic_spread(roc)

        # Log spread changes
        if self.current_spread_bps != self.last_spread_bps:
            direction = "WIDENED" if self.current_spread_bps > self.last_spread_bps else "TIGHTENED"
            logger.info(f"  📊 SPREAD {direction}: {self.last_spread_bps:.1f} → {self.current_spread_bps:.1f} bps (ROC: {roc:+.1f})")
            self.last_spread_bps = self.current_spread_bps

        spread_pct = self.current_spread_bps / 10000
        # DYNAMIC BALANCE: Fetch fresh balance for inventory calculations
        # (Don't use cached self.capital - balance may have changed from deposits/withdrawals)
        current_balance = await self.sdk.get_balance() or self.capital
        max_inventory = current_balance * (self.max_inventory_pct / 100)

        # Inventory ratio (signed: positive=long, negative=short)
        inv_ratio = self.position_notional / max_inventory if max_inventory > 0 else 0

        # v8 INVENTORY SKEW LOGIC
        buy_mult = 1.0
        sell_mult = 1.0
        min_mult = self.min_notional / self.order_size_usd if self.order_size_usd > 0 else 1.0

        if inv_ratio > 0.7:  # Heavy LONG - ONLY sells
            buy_mult = 0.0
            # Use min_notional to reduce - leave buffer for rounding
            # Max sell = position + max_inventory - buffer for rounding
            max_sell = abs(self.position_notional) + max_inventory - 10  # $10 buffer
            sell_target = min(max_sell, self.order_size_usd * 1.5)
            sell_target = max(sell_target, self.min_notional)  # At least min order
            sell_mult = sell_target / self.order_size_usd if self.order_size_usd > 0 else 1.0
            logger.info(f"  REDUCE LONG MODE: {inv_ratio*100:.0f}% - sells only (${sell_target:.0f})")
        elif inv_ratio > 0.3:
            buy_mult = max(min_mult, 0.3)
            sell_mult = 1.3
        elif inv_ratio < -0.7:  # Heavy SHORT - ONLY buys
            # Max buy = position + max_inventory - buffer for rounding
            max_buy = abs(self.position_notional) + max_inventory - 10  # $10 buffer
            buy_target = min(max_buy, self.order_size_usd * 1.5)
            buy_target = max(buy_target, self.min_notional)  # At least min order
            buy_mult = buy_target / self.order_size_usd if self.order_size_usd > 0 else 1.0
            sell_mult = 0.0
            logger.info(f"  REDUCE SHORT MODE: {inv_ratio*100:.0f}% - buys only (${buy_target:.0f})")
        elif inv_ratio < -0.3:
            buy_mult = 1.3
            sell_mult = max(min_mult, 0.3)

        orders_placed = 0

        # Place BUY orders
        if not (self.orders_paused and self.pause_side == 'BUY') and buy_mult > 0:
            for i in range(1, self.num_levels + 1):
                price = mid_price * (1 - spread_pct * i)
                price = self._round_price(price)

                target_notional = self.order_size_usd * buy_mult
                if target_notional < self.min_notional:
                    continue

                size = target_notional / price
                size = self._round_size(size)

                actual_notional = price * size
                if actual_notional < self.min_notional:
                    continue

                # Check inventory limit
                potential = self.position_notional + actual_notional
                if potential > max_inventory:
                    logger.debug(f"  BUY L{i} skipped: potential ${potential:.0f} > max ${max_inventory:.0f}")
                    continue

                try:
                    result = await self.sdk.create_limit_order(
                        symbol=self.symbol,
                        is_buy=True,
                        amount=size,
                        price=price,
                        order_type="POST_ONLY"  # Maker-only: reject if would cross spread
                    )
                    if result and result.get('status') == 'success':
                        digest = result.get('data', {}).get('digest', str(time.time()))
                        self.open_orders[digest] = {
                            'side': 'BUY', 'price': price, 'size': size
                        }
                        orders_placed += 1
                        logger.debug(f"  BUY {size:.4f} @ ${price:,.2f}")
                except Exception as e:
                    logger.debug(f"BUY L{i} error: {e}")

        # Place SELL orders
        if not (self.orders_paused and self.pause_side == 'SELL') and sell_mult > 0:
            for i in range(1, self.num_levels + 1):
                price = mid_price * (1 + spread_pct * i)
                price = self._round_price(price)

                target_notional = self.order_size_usd * sell_mult
                if target_notional < self.min_notional:
                    continue

                size = target_notional / price
                size = self._round_size(size)

                actual_notional = price * size
                if actual_notional < self.min_notional:
                    continue

                # Check inventory limit
                potential = self.position_notional - actual_notional
                if potential < -max_inventory:
                    continue

                try:
                    result = await self.sdk.create_limit_order(
                        symbol=self.symbol,
                        is_buy=False,
                        amount=size,
                        price=price,
                        order_type="POST_ONLY"  # Maker-only: reject if would cross spread
                    )
                    if result and result.get('status') == 'success':
                        digest = result.get('data', {}).get('digest', str(time.time()))
                        self.open_orders[digest] = {
                            'side': 'SELL', 'price': price, 'size': size
                        }
                        orders_placed += 1
                        logger.debug(f"  SELL {size:.4f} @ ${price:,.2f}")
                except Exception as e:
                    logger.debug(f"SELL L{i} error: {e}")

        self.grid_center = mid_price
        if orders_placed > 0:
            logger.info(f"  Grid: {orders_placed} orders @ ${mid_price:,.2f} (spread: {self.current_spread_bps:.1f}bps)")
            # v14: Set cooldown to allow orders to propagate before checking fills
            self.skip_fill_check_cycles = 3  # Skip fill check for 3 seconds

    def _check_fill_rate_alert(self):
        """
        NP-004: Check fill rate and alert if 0 fills for >30 minutes.
        """
        if self.last_fill_time is None:
            # No fills yet - check since start
            if self.start_time:
                time_since_start = (datetime.now() - self.start_time).total_seconds()
                if time_since_start >= self.no_fill_alert_threshold_seconds and not self.no_fill_alert_triggered:
                    logger.warning(f"  ⚠️ NO FILLS ALERT: 0 fills in {time_since_start/60:.0f} minutes since start!")
                    self.no_fill_alert_triggered = True
        else:
            time_since_fill = (datetime.now() - self.last_fill_time).total_seconds()
            if time_since_fill >= self.no_fill_alert_threshold_seconds and not self.no_fill_alert_triggered:
                logger.warning(f"  ⚠️ NO FILLS ALERT: 0 fills in {time_since_fill/60:.0f} minutes!")
                self.no_fill_alert_triggered = True

    def _get_fill_rate_per_hour(self) -> float:
        """
        NP-004: Calculate fills per hour based on runtime.
        """
        if not self.start_time:
            return 0.0
        elapsed_hours = (datetime.now() - self.start_time).total_seconds() / 3600
        if elapsed_hours < 0.01:  # Less than 36 seconds
            return 0.0
        return self.fills_count / elapsed_hours

    async def _check_fills(self) -> int:
        """Check for filled orders using Archive API (v20 - real exchange data).

        Queries the Nado Archive API for actual match records instead of
        guessing fills from vanished orders (which produced 350x over-counting).
        """
        fills = 0
        try:
            # Query real matches from Archive API
            payload = {
                "matches": {
                    "subaccounts": [self.sdk._get_subaccount_bytes32()],
                    "limit": 50,
                    "isolated": False
                }
            }
            response = await self.sdk._archive_query(payload)

            if "error" in response:
                logger.error(f"Archive API error: {response['error']}")
                return 0

            matches = response.get("matches", [])

            if not matches:
                return 0

            # Find new matches since last check
            new_matches = []
            if self.last_submission_idx is None:
                # First check - just record the latest idx, don't count historical
                self.last_submission_idx = matches[0].get("submission_idx")
                logger.info(f"  Fill tracking initialized (latest idx: {self.last_submission_idx})")
                return 0

            for match in matches:
                idx = match.get("submission_idx")
                if idx and int(idx) > int(self.last_submission_idx):
                    new_matches.append(match)

            if new_matches:
                # Update last seen idx
                self.last_submission_idx = max(m.get("submission_idx") for m in new_matches)

                for match in new_matches:
                    # Parse x18 format values
                    base_filled = abs(float(match.get("base_filled", "0"))) / 1e18
                    quote_filled = abs(float(match.get("quote_filled", "0"))) / 1e18
                    fee = abs(float(match.get("fee", "0"))) / 1e18
                    is_taker = match.get("is_taker", False)
                    order_amount = float(match.get("order", {}).get("amount", "0")) / 1e18
                    side = "BUY" if order_amount > 0 else "SELL"

                    # quote_filled is the notional value
                    notional = quote_filled
                    self.total_volume += notional
                    self.fills_count += 1
                    fills += 1

                    self.last_fill_time = datetime.now()
                    self.no_fill_alert_triggered = False

                    price = notional / base_filled if base_filled > 0 else 0
                    logger.info(f"  FILL: {side} {base_filled:.6f} @ ${price:,.2f} (${notional:,.2f}) fee=${fee:.4f} {'TAKER' if is_taker else 'MAKER'}")

            # Sync open_orders: remove stale entries (but do NOT count as fills)
            orders = await self.sdk.get_orders(self.product_id)
            exchange_digests = set()
            for order in orders:
                digest = order.get('digest')
                status = order.get('status', '').upper()
                if status in ['OPEN', 'PENDING', 'NEW']:
                    exchange_digests.add(digest)

            stale = [d for d in self.open_orders if d not in exchange_digests]
            for digest in stale:
                del self.open_orders[digest]

        except Exception as e:
            logger.error(f"Check fills error: {e}")

        return fills

    # ═══════════════════════════════════════════════════════════════════════════
    # LLM TRADING METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    async def _initialize_llm(self):
        """Initialize LLM trading components"""
        logger.info("")
        logger.info("=" * 70)
        logger.info("🤖 INITIALIZING LLM TRADING")
        logger.info("=" * 70)

        try:
            if not self.llm_api_key:
                logger.warning("  OPEN_ROUTER API key not found - LLM trading disabled")
                self.llm_enabled = False
                return

            logger.info(f"  LLM Model: qwen/qwen3.6-plus:free (via OpenRouter)")
            logger.info(f"  Symbols: {', '.join(self.llm_symbols)}")
            logger.info(f"  Position size: ${self.llm_position_size_usd}")
            logger.info(f"  Max positions: {self.llm_max_positions}")
            logger.info(f"  Check interval: {self.llm_check_interval}s")
            logger.info(f"  Exit rules: +{self.llm_profit_target_pct}% / -{self.llm_stop_loss_pct}% / {self.llm_max_hold_hours}h max")

            # Sync any existing LLM positions from exchange
            await self._sync_llm_positions()

            logger.info("  ✅ LLM trading initialized")
            logger.info("=" * 70)

        except Exception as e:
            logger.error(f"  ❌ LLM initialization failed: {e}")
            self.llm_enabled = False

    async def _sync_llm_positions(self):
        """Sync LLM positions from exchange on startup"""
        try:
            positions = await self.sdk.get_positions()
            if not positions:
                return

            for pos in positions:
                symbol = pos.get('symbol')
                if symbol in self.llm_symbols and symbol not in self.llm_positions:
                    # This is an LLM-managed symbol with an existing position
                    size = float(pos.get('size', 0))
                    if abs(size) > 0:
                        entry_price = float(pos.get('entry_price', 0))
                        side = "LONG" if size > 0 else "SHORT"
                        logger.info(f"  📊 Found existing {symbol} {side}: size={abs(size):.6f} @ ${entry_price:.2f}")
                        self.llm_positions[symbol] = {
                            'entry_price': entry_price,
                            'entry_time': datetime.now(),  # Approximate
                            'size': abs(size),
                            'side': side
                        }
        except Exception as e:
            logger.warning(f"  Could not sync LLM positions: {e}")

    async def _run_llm_cycle(self):
        """Run LLM decision cycle - check for opportunities on BTC/SOL"""
        if not self.llm_enabled or not self.llm_api_key:
            return

        now = datetime.now()

        # Check interval
        if self.llm_last_check:
            elapsed = (now - self.llm_last_check).total_seconds()
            if elapsed < self.llm_check_interval:
                return

        self.llm_last_check = now

        logger.info("")
        logger.info("=" * 70)
        logger.info("🤖 LLM DECISION CYCLE")
        logger.info("=" * 70)

        try:
            # First, check and manage existing positions
            await self._manage_llm_positions()

            # Check if we can open new positions
            current_count = len(self.llm_positions)
            if current_count >= self.llm_max_positions:
                logger.info(f"  Max positions reached ({current_count}/{self.llm_max_positions})")
                return

            # Get market data for LLM symbols
            market_data = {}
            for symbol in self.llm_symbols:
                if symbol in self.llm_positions:
                    continue  # Skip symbols we already have positions in

                data = await self._get_llm_market_data(symbol)
                if data:
                    market_data[symbol] = data

            if not market_data:
                logger.info("  No symbols available for new positions")
                return

            # Build prompt for LLM
            prompt = self._build_llm_prompt(market_data)

            # Get LLM decision via OpenRouter API
            response = await self._call_llm_api(prompt)

            if not response:
                logger.info("  No response from LLM")
                return

            # Parse decision
            decision = self._parse_llm_decision(response, list(market_data.keys()))

            if not decision or decision.get('action') == 'NO_TRADE':
                logger.info(f"  LLM Decision: NO_TRADE")
                return

            # Execute decision
            await self._execute_llm_decision(decision)

        except Exception as e:
            logger.error(f"  LLM cycle error: {e}")

        logger.info("=" * 70)

    async def _get_llm_market_data(self, symbol: str) -> Optional[Dict]:
        """Get market data for a symbol"""
        try:
            # Get product ID for symbol
            product = await self.sdk.get_product_by_symbol(symbol)
            if not product:
                logger.warning(f"  Product not found: {symbol}")
                return None

            product_id = product.get('product_id')

            # Get price from Nado market_price query
            response = await self.sdk._query("market_price", {"product_id": str(product_id)})
            if response.get("status") != "success":
                return None

            data = response.get("data", {})
            bid = self.sdk._from_x18(int(data.get('bid_x18', '0')))
            ask = self.sdk._from_x18(int(data.get('ask_x18', '0')))

            if bid <= 0 or ask <= 0:
                return None

            mid_price = (bid + ask) / 2
            spread_bps = ((ask - bid) / mid_price) * 10000 if mid_price > 0 else 0

            return {
                'symbol': symbol,
                'price': mid_price,
                'bid': bid,
                'ask': ask,
                'spread_bps': spread_bps
            }

        except Exception as e:
            logger.warning(f"  Could not get data for {symbol}: {e}")
            return None

    def _build_llm_prompt(self, market_data: Dict) -> str:
        """Build prompt for LLM to decide on LONG or SHORT opportunities based on trend"""
        # Get current ROC for trend context
        current_roc = self._calculate_roc()
        trend_direction = "UPTREND" if current_roc > 20 else "DOWNTREND" if current_roc < -20 else "SIDEWAYS"

        symbols_info = []
        for symbol, data in market_data.items():
            symbols_info.append(f"- {symbol}: ${data['price']:,.2f} (spread: {data['spread_bps']:.1f} bps)")

        prompt = f"""You are a crypto trend trading assistant. The grid MM is paused due to strong trend - your job is to TRADE THE TREND.

TREND SIGNAL: {trend_direction} (ROC: {current_roc:+.1f} bps)

Available markets:
{chr(10).join(symbols_info)}

Rules:
- UPTREND (ROC > +20): Favor LONG positions
- DOWNTREND (ROC < -20): Favor SHORT positions
- SIDEWAYS: NO_TRADE (let grid MM handle it)
- Confidence must be 0.7+ to trade
- We want to ride the trend, not fight it

Respond in this exact format:
ACTION: [LONG/SHORT/NO_TRADE]
SYMBOL: [BTC-PERP/SOL-PERP/NONE]
CONFIDENCE: [0.0-1.0]
REASON: [Brief 1-2 sentence reason]
"""
        return prompt

    async def _call_llm_api(self, prompt: str) -> Optional[str]:
        """Call OpenRouter API for Qwen LLM"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.llm_api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": "qwen/qwen3.6-plus:free",
                        "messages": [
                            {"role": "system", "content": "You are an expert crypto trader. Be concise."},
                            {"role": "user", "content": prompt}
                        ],
                        "max_tokens": 200,
                        "temperature": 0.3
                    },
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get('choices', [{}])[0].get('message', {}).get('content', '')
                    else:
                        logger.error(f"  LLM API error: {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"  LLM API call failed: {e}")
            return None

    def _parse_llm_decision(self, response: str, available_symbols: List[str]) -> Optional[Dict]:
        """Parse LLM response into a decision dict"""
        try:
            lines = response.strip().split('\n')
            decision = {}

            for line in lines:
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip().upper()
                    value = value.strip()

                    if key == 'ACTION':
                        decision['action'] = value.upper()
                    elif key == 'SYMBOL':
                        decision['symbol'] = value.upper()
                    elif key == 'CONFIDENCE':
                        try:
                            decision['confidence'] = float(value)
                        except:
                            decision['confidence'] = 0.5
                    elif key == 'REASON':
                        decision['reason'] = value

            # Validate
            if decision.get('action') not in ['LONG', 'SHORT', 'NO_TRADE']:
                return None

            if decision.get('action') in ('LONG', 'SHORT'):
                symbol = decision.get('symbol')
                if symbol not in available_symbols:
                    logger.warning(f"  Invalid symbol: {symbol}")
                    return None

                confidence = decision.get('confidence', 0)
                if confidence < 0.7:
                    logger.info(f"  Confidence too low: {confidence:.2f} < 0.70")
                    return {'action': 'NO_TRADE'}

            return decision

        except Exception as e:
            logger.warning(f"  Parse error: {e}")
            return None

    async def _execute_llm_decision(self, decision: Dict):
        """Execute an LLM trading decision (LONG or SHORT)"""
        action = decision.get('action')
        symbol = decision.get('symbol')
        confidence = decision.get('confidence', 0)
        reason = decision.get('reason', 'No reason provided')

        if action not in ('LONG', 'SHORT') or not symbol:
            return

        emoji = "📈" if action == "LONG" else "📉"
        logger.info(f"  {emoji} LLM Decision: {action} {symbol}")
        logger.info(f"     Confidence: {confidence:.2f}")
        logger.info(f"     Reason: {reason[:80]}...")

        try:
            # Get current price
            market_data = await self._get_llm_market_data(symbol)
            if not market_data:
                logger.error(f"  Could not get price for {symbol}")
                return

            # Buy at ask, sell at bid
            price = market_data['ask'] if action == 'LONG' else market_data['bid']
            side = "buy" if action == "LONG" else "sell"

            # Calculate size
            size = self.llm_position_size_usd / price

            # Get product info for proper sizing
            product = await self.sdk.get_product_by_symbol(symbol)
            if product:
                step = float(product.get('base_currency_increment', 0.0001))
                size = round(size / step) * step

            logger.info(f"  Placing {action} order: {size:.6f} {symbol} @ ${price:.2f}")

            # Place order using correct SDK method
            is_buy = (action == "LONG")
            result = await self.sdk.create_limit_order(
                symbol=symbol,
                is_buy=is_buy,
                amount=size,
                price=price,
                order_type="DEFAULT",
                reduce_only=False
            )

            if result and result.get('status') != 'failure' and result.get('orderId'):
                logger.info(f"  ✅ Order placed: {result.get('orderId')}")
                # Track position
                self.llm_positions[symbol] = {
                    'entry_price': price,
                    'entry_time': datetime.now(),
                    'size': size,
                    'side': action
                }
            else:
                error = result.get('error', 'Unknown') if result else 'No result'
                logger.error(f"  ❌ Order failed: {error}")

        except Exception as e:
            logger.error(f"  Execution error: {e}")

    async def _manage_llm_positions(self):
        """Check and manage existing LLM positions - exit if rules triggered"""
        if not self.llm_positions:
            return

        positions_to_close = []

        for symbol, pos_data in self.llm_positions.items():
            entry_price = pos_data['entry_price']
            entry_time = pos_data['entry_time']
            size = pos_data['size']
            side = pos_data.get('side', 'LONG')

            # Get current price
            market_data = await self._get_llm_market_data(symbol)
            if not market_data:
                continue

            # For LONG: close at bid, profit when price up
            # For SHORT: close at ask, profit when price down
            if side == 'LONG':
                current_price = market_data['bid']
                pnl_pct = ((current_price - entry_price) / entry_price) * 100
            else:  # SHORT
                current_price = market_data['ask']
                pnl_pct = ((entry_price - current_price) / entry_price) * 100

            hold_hours = (datetime.now() - entry_time).total_seconds() / 3600

            logger.info(f"  📊 {symbol} {side}: P&L {pnl_pct:+.2f}% | Hold: {hold_hours:.1f}h")

            # Check exit conditions
            exit_reason = None

            if pnl_pct >= self.llm_profit_target_pct:
                exit_reason = f"PROFIT TARGET (+{pnl_pct:.2f}%)"
            elif pnl_pct <= -self.llm_stop_loss_pct:
                exit_reason = f"STOP LOSS ({pnl_pct:.2f}%)"
            elif hold_hours >= self.llm_max_hold_hours:
                exit_reason = f"MAX HOLD TIME ({hold_hours:.1f}h)"

            if exit_reason:
                positions_to_close.append((symbol, size, exit_reason, current_price))

        # Close positions
        for symbol, size, reason, price in positions_to_close:
            logger.info(f"  🔻 Closing {symbol}: {reason}")
            await self._close_llm_position(symbol, size, price)

    async def _close_llm_position(self, symbol: str, size: float, price: float):
        """Close an LLM position (LONG or SHORT)"""
        try:
            # Determine close side based on position side
            pos_info = self.llm_positions.get(symbol, {})
            pos_side = pos_info.get('side', 'LONG')
            is_buy = (pos_side == "SHORT")  # Buy to close SHORT, sell to close LONG

            result = await self.sdk.create_limit_order(
                symbol=symbol,
                is_buy=is_buy,
                amount=size,
                price=price,
                order_type="DEFAULT",
                reduce_only=True
            )

            if result and result.get('status') != 'failure' and result.get('orderId'):
                logger.info(f"  ✅ Position closed ({pos_side})")
                if symbol in self.llm_positions:
                    del self.llm_positions[symbol]
            else:
                error = result.get('error', 'Unknown') if result else 'No result'
                logger.error(f"  ❌ Close failed: {error}")

        except Exception as e:
            logger.error(f"  Close error: {e}")

    async def run(self):
        """Main trading loop - v15 with time-based refresh (US-001)"""
        try:
            if not await self.initialize():
                return

            logger.info(f"\nStarting grid MM (reset on {self.grid_reset_pct}% move)...")
            logger.info("-" * 70)

            # Place initial grid
            await self._place_grid_orders(self.grid_center)
            self.last_refresh_time = datetime.now()  # v15: Track refresh time

            cycle = 0
            while True:
                cycle += 1

                # Get current price
                mid = await self._get_mid_price()
                if not mid:
                    await asyncio.sleep(1)
                    continue

                self.price_history.append(mid)

                # Calculate ROC and update pause state
                roc = self._calculate_roc()
                self._update_pause_state(roc)

                # v18: Update dynamic spread EVERY cycle (not just when placing orders)
                # This fixes bug where spread gets stuck when no fills/price moves
                new_spread = self._calculate_dynamic_spread(roc)
                if new_spread != self.current_spread_bps:
                    direction = "WIDENED" if new_spread > self.current_spread_bps else "TIGHTENED"
                    logger.info(f"  📊 SPREAD {direction}: {self.current_spread_bps:.1f} → {new_spread:.1f} bps (ROC: {roc:+.1f})")
                    self.current_spread_bps = new_spread

                # v14: Decrement cooldown and skip fill check if needed
                if self.skip_fill_check_cycles > 0:
                    self.skip_fill_check_cycles -= 1
                    fills = 0  # Don't check fills during cooldown
                else:
                    # Check for fills
                    fills = await self._check_fills()

                # v8: Price-based grid reset (0.25% move)
                price_move_pct = abs(mid - self.grid_center) / self.grid_center * 100 if self.grid_center else 0

                # Inventory ratio for force reset (use fresh balance)
                loop_balance = await self.sdk.get_balance() or self.capital
                max_inventory = loop_balance * (self.max_inventory_pct / 100)
                inventory_ratio = abs(self.position_notional) / max_inventory if max_inventory > 0 else 0

                # NP-003: Check for inventory rebalance (Nado-specific due to liquidity issues)
                inventory_pct = inventory_ratio * 100

                # DEBUG: Log values for rebalance check (remove after debugging)
                if cycle % 30 == 0:  # Log every 30 cycles (same as status)
                    logger.info(f"  [REBAL-DBG] notional={self.position_notional:.2f} balance={loop_balance:.2f} max_inv={max_inventory:.2f} ratio={inventory_ratio:.2f} pct={inventory_pct:.0f}%")

                await self._check_inventory_rebalance(inventory_pct, fills)

                # LLM Trading: Check for opportunities on BTC/SOL
                if self.llm_enabled:
                    await self._run_llm_cycle()

                # Safety: refresh if no tracked orders
                no_tracked = len(self.open_orders) == 0
                not_paused = not (self.orders_paused and self.pause_side == 'ALL')

                # NOTE: Removed inventory_ratio > 0.8 reset trigger (v14)
                # REDUCE LONG/SHORT MODE already handles high inventory by placing one-sided orders
                # Resetting every cycle was cancelling orders before they could fill
                #
                # v14: Also removed "no active orders" reset trigger
                # Nado API has propagation delays - orders aren't immediately visible in get_orders()
                # This was causing false "no active orders" detection and constant reset loops
                #
                # v15: Added time-based refresh (US-001) to prevent stale orders
                # Hibachi uses 30s refresh and works correctly - apply similar pattern
                time_since_refresh = (datetime.now() - self.last_refresh_time).total_seconds() if self.last_refresh_time else 0
                time_based_refresh = time_since_refresh >= self.time_refresh_interval

                # v16: Check for stale orders (US-003)
                # If any tracked order is >0.2% from mid price, trigger immediate refresh
                stale_order_info = self._check_stale_orders(mid)
                stale_order_refresh = stale_order_info is not None

                should_refresh = (
                    fills > 0 or
                    price_move_pct >= self.grid_reset_pct or
                    time_based_refresh or
                    stale_order_refresh
                )

                if should_refresh:
                    if stale_order_refresh:
                        stale_price, stale_dist = stale_order_info
                        logger.info(f"  Stale order refresh: order at ${stale_price:,.2f} is {stale_dist:.2f}% from mid")
                    elif time_based_refresh:
                        logger.info(f"  Time-based refresh: {time_since_refresh:.0f}s since last placement")
                    elif price_move_pct >= self.grid_reset_pct:
                        logger.info(f"  Grid reset: price moved {price_move_pct:.3f}%")
                    await self._sync_position()
                    await self._place_grid_orders(mid, roc)
                    self.last_refresh_time = datetime.now()  # v15: Update refresh time

                # NP-004: Check fill rate alert
                self._check_fill_rate_alert()

                # Status log every 30 cycles
                if cycle % 30 == 0:
                    balance = await self.sdk.get_balance() or 0
                    pnl = balance - self.initial_balance
                    elapsed = (datetime.now() - self.start_time).total_seconds() / 60
                    inv_pct = abs(self.position_notional) / self.capital * 100 if self.capital > 0 else 0
                    pause_status = f"PAUSE-{self.pause_side}" if self.orders_paused else "LIVE"

                    # NP-004: Calculate fill rate
                    fill_rate = self._get_fill_rate_per_hour()

                    logger.info(f"\n[{elapsed:.1f}m] ${mid:,.2f} | ROC: {roc:+.1f}bps | Spread: {self.current_spread_bps:.1f}bps | {pause_status}")
                    logger.info(f"  Position: {self.position_size:.6f} ({inv_pct:.0f}% inv)")
                    logger.info(f"  Volume: ${self.total_volume:,.2f} | Fills: {self.fills_count} ({fill_rate:.1f}/hr)")
                    logger.info(f"  P&L: ${pnl:+.2f} (${balance:.2f})")

                await asyncio.sleep(1)

        except KeyboardInterrupt:
            logger.info("\nStopping...")
        except Exception as e:
            logger.error(f"Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            logger.info("\nCancelling orders...")
            await self._cancel_all_orders()
            self._print_report()

    def _print_report(self):
        """Print final report"""
        elapsed = (datetime.now() - self.start_time).total_seconds() / 60 if self.start_time else 0
        logger.info("\n" + "=" * 70)
        logger.info("NADO GRID MM v17 - FINAL REPORT")
        logger.info("=" * 70)
        logger.info(f"Duration: {elapsed:.1f} minutes")
        logger.info(f"Total Volume: ${self.total_volume:,.2f}")
        logger.info(f"Total Fills: {self.fills_count}")
        logger.info("=" * 70)


async def main():
    mm = GridMarketMakerNado(
        symbol="ETH-PERP",
        base_spread_bps=1.5,         # v19: aggressive fills (dynamic overrides this anyway)
        order_size_usd=100.0,        # $100 = Nado minimum
        num_levels=2,                # v18: 2 levels per side (Qwen recommendation)
        max_inventory_pct=400.0,     # 4x leverage - needed for $100 orders on $40 balance
        capital=50.0,                # Ignored - uses dynamic balance
        roc_threshold_bps=50.0,      # v14: back to 50 (20 was too aggressive - caused constant pauses)
        min_pause_duration=120,      # v13: shorter pause (was 300) - resume faster in ranges
    )
    await mm.run()


if __name__ == "__main__":
    asyncio.run(main())
