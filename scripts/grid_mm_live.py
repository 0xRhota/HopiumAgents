#!/usr/bin/env python3.11
"""
Grid Market Maker v17 - LIVE on Paradex
Dynamic spread based on ROC volatility + time-based refresh + stale order detection + tight spread mode

Strategy: Place limit orders on both sides of mid price
- Earns spread + maker rebates (-0.005%)
- DYNAMIC SPREAD: Automatically adjusts based on volatility
- TIME-BASED REFRESH: Refresh orders every 5 minutes (US-002)
- STALE ORDER DETECTION: Refresh if orders drift >0.2% from mid (US-003)
- TIGHT SPREAD MODE: Reduce spread by 20% after 2 min of calm market (NP-002)

v17 Changes (Tight spread mode - NP-002):
- Track consecutive low-ROC cycles (ROC < 2bps)
- After 2 minutes of low ROC, reduce spread by 20%
- Log shows 'TIGHT_SPREAD mode: spread reduced to Xbps'
- Revert to normal spread when ROC increases above 5bps

v16 Changes (Stale order detection - US-003):
- Added _check_stale_orders() method to detect orders >0.2% from mid
- Triggers immediate refresh when stale orders detected
- Log shows 'Stale order refresh: order at $X is Y% from mid'

v15 Changes (Time-based refresh - US-002):
- Added last_refresh_time tracking
- Refresh orders if >5 minutes since last placement
- Log shows 'Time-based refresh' when triggered

v12 Parameters (Dynamic Spread):
- Spread: DYNAMIC based on ROC
  - ROC 0-5 bps → 1.5 bps spread (calm market, max fills)
  - ROC 5-15 bps → 3 bps spread (low volatility)
  - ROC 15-30 bps → 6 bps spread (moderate volatility)
  - ROC 30-50 bps → 10 bps spread (high volatility)
  - ROC >50 bps → PAUSE orders (existing pause logic)
- ROC threshold: 50 bps for pause
- Pause duration: 5 minutes
- Grid reset: 0.5% price move OR 5 min time-based refresh
"""

import os
import sys
import time
import asyncio
import logging
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import deque
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(project_root, '.env'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

from paradex_py import ParadexSubkey
from paradex_py.common.order import Order, OrderType, OrderSide
from llm_agent.self_learning import SelfLearning


class GridMarketMakerLive:
    """
    Live Grid Market Maker v16 for Paradex
    Dynamic spread based on ROC volatility + time-based refresh (US-002) + stale order detection (US-003)
    """

    def __init__(
        self,
        symbol: str = "BTC-USD-PERP",
        base_spread_bps: float = 8.0,      # v11: 8 bps (15 too wide) per Qwen
        order_size_usd: float = 50.0,      # $50 per order - use leverage
        num_levels: int = 2,               # 2 levels per side = $100 per side
        duration_minutes: int = 60,
        max_inventory_pct: float = 100.0,  # v10: lower leverage
        capital: float = 73.0,
        roc_threshold_bps: float = 50.0,   # v10: real trends only per Qwen
        min_pause_duration: int = 300,     # v10: 5 min pause per Qwen
    ):
        self.symbol = symbol
        self.base_spread_bps = base_spread_bps
        self.order_size_usd = order_size_usd
        self.num_levels = num_levels
        self.duration_minutes = duration_minutes
        self.max_inventory_pct = max_inventory_pct
        self.capital = capital
        self.roc_threshold_bps = roc_threshold_bps
        self.min_pause_duration = min_pause_duration

        # State
        self.client = None
        self.grid_center = None
        self.open_orders: Dict[str, Dict] = {}  # order_id -> order info
        self.price_history: deque = deque(maxlen=360)  # 6 minutes at 1/sec

        # Trend detection
        self.orders_paused = False
        self.pause_side = None
        self.pause_start_time = None

        # Dynamic spread tracking
        self.current_spread_bps = base_spread_bps
        self.last_spread_bps = base_spread_bps

        # v15: Time-based refresh (US-002)
        self.last_refresh_time = None
        self.time_refresh_interval = 300  # 5 minutes

        # v17: Tight spread mode (NP-002)
        self.tight_spread_mode = False
        self.low_roc_start_time = None
        self.tight_spread_threshold_bps = 2.0  # ROC < 2 bps = calm market
        self.tight_spread_exit_threshold_bps = 5.0  # ROC > 5 bps = exit tight mode
        self.tight_spread_activation_seconds = 120  # 2 minutes of calm
        self.tight_spread_reduction_pct = 0.20  # Reduce spread by 20%

        # Stats
        self.total_volume = 0.0
        self.realized_pnl = 0.0
        self.fills_count = 0
        self.start_time = None
        self.initial_balance = 0.0
        self.current_balance = 0.0

        # NP-004: Fill rate monitoring
        self.last_fill_time = None
        self.no_fill_alert_threshold_seconds = 1800  # Alert if 0 fills for 30 minutes
        self.no_fill_alert_triggered = False

        # v20: Real fill tracking via fetch_fills API (replaces broken vanished-order detection)
        self.last_fill_id = None  # Track last seen fill ID

        # Position tracking
        self.position_size = 0.0
        self.position_notional = 0.0

        # Market info
        self.tick_size = 1.0  # BTC tick size
        self.step_size = 0.0001  # BTC step size
        self.min_notional = 10.0  # Paradex minimum order notional

        # Self-learning (for user notes / working memory)
        self.last_self_learning_time = datetime.now()
        self.self_learning_interval = 1800  # 30 minutes

    def _run_self_learning_check(self):
        """Check and log user notes + performance (every 30 min)"""
        notes = SelfLearning.get_active_notes()
        if notes:
            logger.info("")
            logger.info("=" * 50)
            logger.info("📚 SELF-LEARNING CHECK-IN (Working Memory)")
            logger.info("=" * 50)
            for note in notes:
                ts = note.get('timestamp', '')[:16].replace('T', ' ')
                msg = note.get('message', '')[:100]
                logger.info(f"  [{ts}] {msg}")
            logger.info("=" * 50)
        self.last_self_learning_time = datetime.now()

    def initialize(self):
        """Initialize Paradex client with authentication"""
        logger.info("=" * 70)
        logger.info("GRID MARKET MAKER v15 - DYNAMIC SPREAD + TIME REFRESH")
        logger.info("=" * 70)
        logger.info(f"Symbol: {self.symbol}")
        logger.info(f"Spread: DYNAMIC (1.5-15 bps based on ROC)")
        logger.info(f"  ROC 0-5 bps → 1.5 bps spread")
        logger.info(f"  ROC 5-15 bps → 3 bps spread")
        logger.info(f"  ROC 15-30 bps → 6 bps spread")
        logger.info(f"  ROC 30-50 bps → 10 bps spread")
        logger.info(f"  ROC >50 bps → PAUSE orders")
        logger.info(f"Order Size: ${self.order_size_usd}")
        logger.info(f"Levels: {self.num_levels} per side")
        logger.info(f"Capital: ${self.capital}")
        logger.info(f"Max Inventory: {self.max_inventory_pct}%")
        logger.info(f"ROC Threshold: {self.roc_threshold_bps} bps")
        logger.info(f"Min Pause: {self.min_pause_duration}s")
        logger.info(f"Time-based Refresh: {self.time_refresh_interval}s")
        logger.info("=" * 70)

        # Initialize authenticated client
        private_key = os.getenv("PARADEX_PRIVATE_SUBKEY")
        if not private_key:
            raise ValueError("PARADEX_PRIVATE_SUBKEY not set in .env")

        logger.info("Connecting to Paradex...")
        self.client = ParadexSubkey(
            env='prod',
            l2_private_key=private_key,
            l2_address=os.getenv('PARADEX_ACCOUNT_ADDRESS'),
        )

        # Get account balance
        account = self.client.api_client.fetch_account_summary()
        self.initial_balance = float(account.account_value)
        self.current_balance = self.initial_balance
        logger.info(f"Account balance: ${self.initial_balance:.2f}")

        # Use actual balance as capital (dynamic, not hardcoded)
        self.capital = self.initial_balance

        # Get market info
        markets = self.client.api_client.fetch_markets()
        for m in markets.get('results', []):
            if m.get('symbol') == self.symbol:
                self.tick_size = float(m.get('price_tick_size', 1.0))
                self.step_size = float(m.get('order_size_increment', 0.0001))
                self.min_notional = float(m.get('min_notional', 10))
                logger.info(f"Market: tick={self.tick_size}, step={self.step_size}, min_notional=${self.min_notional}")
                break

        # Get current price
        bbo = self.client.api_client.fetch_bbo(market=self.symbol)
        if not bbo:
            raise Exception(f"Cannot fetch BBO for {self.symbol}")

        bid = float(bbo['bid'])
        ask = float(bbo['ask'])
        mid = (bid + ask) / 2
        logger.info(f"Initial price: ${mid:,.2f} (bid: ${bid:,.2f}, ask: ${ask:,.2f})")

        self.grid_center = mid
        self.price_history.append(mid)
        self.start_time = datetime.now()

        # Check existing positions
        self._sync_position()

        # TASK 8: Close any existing position before starting fresh grid
        if self.position_size != 0:
            logger.warning("=" * 70)
            logger.warning("EXISTING POSITION DETECTED - CLOSING BEFORE GRID START")
            logger.warning("=" * 70)
            self._close_all_positions()
            self._sync_position()

        return True

    def _sync_position(self):
        """Sync position from exchange"""
        try:
            positions = self.client.api_client.fetch_positions()
            self.position_size = 0.0
            self.position_notional = 0.0

            if positions and positions.get('results'):
                for pos in positions['results']:
                    if pos.get('market') == self.symbol:
                        size = float(pos.get('size', 0))
                        if size != 0:
                            self.position_size = size
                            entry = float(pos.get('average_entry_price', 0))
                            # SIGNED notional: positive for long, negative for short
                            self.position_notional = size * entry
                            side = 'LONG' if size > 0 else 'SHORT'
                            logger.info(f"Existing position: {side} {abs(size):.6f} @ ${entry:,.2f} (${abs(self.position_notional):,.2f})")
        except Exception as e:
            logger.error(f"Position sync error: {e}")

    def _close_all_positions(self):
        """
        TASK 8: Close any existing position with a market order
        This prevents inheriting losing positions from previous runs
        """
        try:
            if self.position_size == 0:
                logger.info("No position to close")
                return

            side = 'LONG' if self.position_size > 0 else 'SHORT'
            abs_size = abs(self.position_size)

            logger.info(f"Closing {side} position: {abs_size:.6f} BTC")

            # Market order to close - opposite side
            close_side = OrderSide.Sell if self.position_size > 0 else OrderSide.Buy

            order = Order(
                market=self.symbol,
                order_type=OrderType.Market,
                order_side=close_side,
                size=self._round_size(abs_size),
                client_id=f"close_pos_{int(time.time())}",
            )

            result = self.client.api_client.submit_order(order)
            if result.get('status') in ['NEW', 'OPEN', 'CLOSED']:
                fill_price = float(result.get('avg_fill_price', 0))
                logger.info(f"  Position closed @ ${fill_price:,.2f}")
                self.position_size = 0.0
                self.position_notional = 0.0
            else:
                logger.error(f"  Close order rejected: {result}")

        except Exception as e:
            logger.error(f"Close position error: {e}")

    def _round_price(self, price: float) -> Decimal:
        """Round price to tick size"""
        tick_dec = Decimal(str(self.tick_size))
        price_dec = Decimal(str(price))
        # Round to nearest tick
        rounded = (price_dec / tick_dec).quantize(Decimal('1')) * tick_dec
        return rounded

    def _round_size(self, size: float) -> Decimal:
        """Round size to step size"""
        step_dec = Decimal(str(self.step_size))
        size_dec = Decimal(str(size))
        # Round down to step size
        rounded = (size_dec / step_dec).quantize(Decimal('1'), rounding='ROUND_DOWN') * step_dec
        return max(rounded, step_dec)

    def _calculate_roc(self) -> float:
        """Calculate Rate of Change in bps over 3-minute window"""
        if len(self.price_history) < 180:
            return 0.0  # Need 3 min of data before calculating ROC

        prices = list(self.price_history)
        current = prices[-1]
        past = prices[-180]  # 3 minutes ago (180 samples at 1/sec)

        if past == 0:
            return 0.0

        return (current - past) / past * 10000

    def _calculate_dynamic_spread(self, roc: float) -> float:
        """
        Calculate dynamic spread based on ROC (volatility).

        Returns spread in bps. Lower ROC = tighter spread (more fills).
        Higher ROC = wider spread (protect from adverse selection).

        v17 (NP-002): Apply tight spread reduction when in calm market mode

        Spread bands:
        | ROC (abs) | Spread | Rationale |
        |-----------|--------|-----------|
        | 0-5 bps   | 1.5 bps| Calm market, max fills |
        | 5-15 bps  | 3 bps  | Low volatility, balanced |
        | 15-30 bps | 6 bps  | Moderate volatility, protect |
        | 30-50 bps | 10 bps | High volatility, wide protection |
        | > 50 bps  | PAUSE  | Handled by existing pause logic |
        """
        abs_roc = abs(roc)

        if abs_roc < 5:
            spread = 1.5
        elif abs_roc < 15:
            spread = 3.0
        elif abs_roc < 30:
            spread = 6.0
        elif abs_roc < 50:
            spread = 10.0
        else:
            # Above 50 bps is handled by pause logic, but return wide spread as fallback
            spread = 15.0

        # NP-002: Apply tight spread reduction in calm markets
        if self.tight_spread_mode and abs_roc < self.tight_spread_exit_threshold_bps:
            spread = spread * (1 - self.tight_spread_reduction_pct)

        return spread

    def _update_tight_spread_mode(self, roc: float):
        """
        NP-002: Update tight spread mode based on low-ROC periods.

        - Track consecutive low-ROC cycles (ROC < 2bps)
        - After 2 minutes of low ROC, activate tight spread mode
        - Revert to normal spread when ROC increases above 5bps
        """
        abs_roc = abs(roc)

        if abs_roc < self.tight_spread_threshold_bps:
            # Low volatility - track duration
            if self.low_roc_start_time is None:
                self.low_roc_start_time = datetime.now()

            elapsed = (datetime.now() - self.low_roc_start_time).total_seconds()
            if elapsed >= self.tight_spread_activation_seconds and not self.tight_spread_mode:
                self.tight_spread_mode = True
                logger.info(f"  📉 TIGHT_SPREAD mode: activated after {elapsed:.0f}s of calm (ROC: {roc:+.1f}bps)")
        elif abs_roc > self.tight_spread_exit_threshold_bps:
            # Volatility returned - exit tight mode
            if self.tight_spread_mode:
                logger.info(f"  📈 TIGHT_SPREAD mode: deactivated (ROC: {roc:+.1f}bps > {self.tight_spread_exit_threshold_bps}bps)")
            self.tight_spread_mode = False
            self.low_roc_start_time = None

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

        for order_id, order_info in self.open_orders.items():
            order_price = order_info.get('price', 0)
            if order_price <= 0:
                continue

            distance_pct = abs(order_price - mid_price) / mid_price * 100

            if distance_pct > stale_threshold_pct:
                return (order_price, distance_pct)

        return None

    def _update_pause_state(self, roc: float):
        """
        TASK 10: Update order pause state based on trend
        IMPROVED: Pause ALL grid activity during strong trends (not just one side)
        This prevents adverse selection where we get filled against the trend
        """
        old_paused = self.orders_paused
        old_side = self.pause_side

        # TASK 10: Strong trend threshold (2x normal) pauses ALL grid activity
        strong_trend_threshold = self.roc_threshold_bps * 2.0  # 2.0 bps

        if abs(roc) > strong_trend_threshold:
            # STRONG TREND - pause ALL orders to avoid adverse selection
            if not self.orders_paused or self.pause_side != 'ALL':
                self.pause_start_time = datetime.now()
            self.orders_paused = True
            self.pause_side = 'ALL'
            direction = "UP" if roc > 0 else "DOWN"
            if not old_paused or old_side != 'ALL':
                logger.warning(f"  ⚡ STRONG TREND {direction} - PAUSE ALL GRID (ROC: {roc:+.2f} bps)")
        elif roc > self.roc_threshold_bps:
            # Uptrend - pause SELL orders (we don't want to sell into rallies)
            if not self.orders_paused or self.pause_side != 'SELL':
                self.pause_start_time = datetime.now()
            self.orders_paused = True
            self.pause_side = 'SELL'
        elif roc < -self.roc_threshold_bps:
            # Downtrend - pause BUY orders (we don't want to buy into dumps)
            if not self.orders_paused or self.pause_side != 'BUY':
                self.pause_start_time = datetime.now()
            self.orders_paused = True
            self.pause_side = 'BUY'
        else:
            # Check if min pause duration met
            if self.orders_paused and self.pause_start_time:
                elapsed = (datetime.now() - self.pause_start_time).total_seconds()
                if elapsed >= self.min_pause_duration:
                    self.orders_paused = False
                    self.pause_side = None
                    self.pause_start_time = None
            else:
                self.orders_paused = False
                self.pause_side = None

        # Log state changes (non-ALL)
        if self.orders_paused and not old_paused and self.pause_side != 'ALL':
            logger.info(f"  PAUSE {self.pause_side} orders (ROC: {roc:+.2f} bps)")
        elif not self.orders_paused and old_paused:
            logger.info(f"  RESUME orders (ROC: {roc:+.2f} bps)")

    def _cancel_all_orders(self):
        """Cancel all open orders"""
        try:
            orders = self.client.api_client.fetch_orders(params={'market': self.symbol})
            if orders and orders.get('results'):
                for order in orders['results']:
                    if order.get('status') in ['NEW', 'OPEN', 'UNTRIGGERED']:
                        order_id = order.get('id')
                        try:
                            self.client.api_client.cancel_order(order_id=order_id)
                        except Exception as e:
                            logger.debug(f"Cancel error for {order_id}: {e}")
            self.open_orders.clear()
        except Exception as e:
            logger.error(f"Cancel all orders error: {e}")

    def _place_grid_orders(self, mid_price: float, roc: float = 0.0):
        """
        Place grid of limit orders with dynamic spread based on volatility (ROC)

        Dynamic spread protects from adverse selection during volatile periods
        while capturing fills during calm markets.
        """
        # First cancel existing orders
        self._cancel_all_orders()

        # TASK 10: If ALL orders paused due to strong trend, don't place anything
        if self.orders_paused and self.pause_side == 'ALL':
            logger.info(f"  Grid SKIPPED: Strong trend detected, all orders paused")
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
        try:
            account = self.client.api_client.fetch_account_summary()
            current_balance = float(account.account_value) if account else self.capital
        except:
            current_balance = self.capital
        max_inventory = current_balance * (self.max_inventory_pct / 100)

        # Calculate inventory ratio (signed: positive=long, negative=short)
        inv_ratio = self.position_notional / max_inventory if max_inventory > 0 else 0

        # TASK 9: IMPROVED INVENTORY MANAGEMENT
        # When heavily positioned (>30% of max), ONLY place orders that REDUCE position
        # This is the KEY fix - don't place orders that could ADD to losing positions

        buy_mult = 1.0
        sell_mult = 1.0
        inventory_mode = "BALANCED"

        # Calculate minimum multiplier to stay above min notional
        min_mult = self.min_notional / self.order_size_usd if self.order_size_usd > 0 else 1.0

        if inv_ratio > 0.7:  # Heavy LONG - ONLY place sells to reduce
            inventory_mode = "REDUCE_LONG"
            buy_mult = 0.0  # No buys! Would add to long
            sell_mult = 1.5  # Aggressive sells to reduce
            logger.info(f"  📉 REDUCE LONG MODE: {inv_ratio*100:.0f}% inventory - sells only")
        elif inv_ratio > 0.3:  # Moderate long - reduce buys, increase sells
            buy_mult = max(min_mult, 0.3)
            sell_mult = 1.3
        elif inv_ratio < -0.7:  # Heavy SHORT - ONLY place buys to reduce
            inventory_mode = "REDUCE_SHORT"
            buy_mult = 1.5  # Aggressive buys to reduce
            sell_mult = 0.0  # No sells! Would add to short
            logger.info(f"  📈 REDUCE SHORT MODE: {inv_ratio*100:.0f}% inventory - buys only")
        elif inv_ratio < -0.3:  # Moderate short - reduce sells, increase buys
            buy_mult = 1.3
            sell_mult = max(min_mult, 0.3)

        orders_placed = 0

        # Place BUY orders (below mid) - skip if paused
        if not (self.orders_paused and self.pause_side == 'BUY') and buy_mult > 0:
            for i in range(1, self.num_levels + 1):
                price = mid_price * (1 - spread_pct * i)
                price_dec = self._round_price(price)

                # Calculate order size with multiplier applied
                target_notional = self.order_size_usd * buy_mult

                # ENFORCE MINIMUM NOTIONAL - skip if below limit
                if target_notional < self.min_notional:
                    logger.debug(f"  Skip BUY L{i}: ${target_notional:.2f} < ${self.min_notional} min")
                    continue

                size = target_notional / float(price_dec)
                size_dec = self._round_size(size)

                # Verify final notional after rounding
                actual_notional = float(price_dec) * float(size_dec)
                if actual_notional < self.min_notional:
                    logger.debug(f"  Skip BUY L{i}: rounded ${actual_notional:.2f} < ${self.min_notional} min")
                    continue

                # Skip if would exceed inventory limit (BUY increases position toward positive)
                # position_notional is signed: positive=long, negative=short
                potential = self.position_notional + actual_notional
                if potential > max_inventory:
                    logger.debug(f"  Skip BUY L{i}: ${potential:.2f} would exceed ${max_inventory:.2f} limit")
                    continue

                try:
                    order = Order(
                        market=self.symbol,
                        order_type=OrderType.Limit,
                        order_side=OrderSide.Buy,
                        size=size_dec,
                        limit_price=price_dec,
                        client_id=f"grid_buy_{i}_{int(time.time())}",
                        instruction="POST_ONLY",  # Maker-only: reject if would cross spread
                    )
                    result = self.client.api_client.submit_order(order)
                    if result.get('status') in ['NEW', 'OPEN']:
                        self.open_orders[result.get('id')] = {
                            'side': 'BUY',
                            'price': float(price_dec),
                            'size': float(size_dec),
                            'level': i
                        }
                        orders_placed += 1
                    else:
                        logger.warning(f"BUY order rejected: {result}"[:100])
                except Exception as e:
                    logger.warning(f"BUY error L{i}: size={size_dec} price={price_dec}: {e}"[:150])

        # Place SELL orders (above mid) - skip if paused
        if not (self.orders_paused and self.pause_side == 'SELL') and sell_mult > 0:
            for i in range(1, self.num_levels + 1):
                price = mid_price * (1 + spread_pct * i)
                price_dec = self._round_price(price)

                # Calculate order size with multiplier applied
                target_notional = self.order_size_usd * sell_mult

                # ENFORCE MINIMUM NOTIONAL - skip if below limit
                if target_notional < self.min_notional:
                    logger.debug(f"  Skip SELL L{i}: ${target_notional:.2f} < ${self.min_notional} min")
                    continue

                size = target_notional / float(price_dec)
                size_dec = self._round_size(size)

                # Verify final notional after rounding
                actual_notional = float(price_dec) * float(size_dec)
                if actual_notional < self.min_notional:
                    logger.debug(f"  Skip SELL L{i}: rounded ${actual_notional:.2f} < ${self.min_notional} min")
                    continue

                # Skip if would exceed inventory limit (SELL decreases position toward negative)
                # position_notional is signed: positive=long, negative=short
                potential = self.position_notional - actual_notional
                if potential < -max_inventory:
                    logger.debug(f"  Skip SELL L{i}: ${potential:.2f} would exceed -${max_inventory:.2f} limit")
                    continue

                try:
                    order = Order(
                        market=self.symbol,
                        order_type=OrderType.Limit,
                        order_side=OrderSide.Sell,
                        size=size_dec,
                        limit_price=price_dec,
                        client_id=f"grid_sell_{i}_{int(time.time())}",
                        instruction="POST_ONLY",  # Maker-only: reject if would cross spread
                    )
                    result = self.client.api_client.submit_order(order)
                    if result.get('status') in ['NEW', 'OPEN']:
                        self.open_orders[result.get('id')] = {
                            'side': 'SELL',
                            'price': float(price_dec),
                            'size': float(size_dec),
                            'level': i
                        }
                        orders_placed += 1
                    else:
                        logger.warning(f"SELL order rejected: {result}"[:100])
                except Exception as e:
                    logger.warning(f"SELL error L{i}: size={size_dec} price={price_dec}: {e}"[:150])

        self.grid_center = mid_price
        if orders_placed > 0:
            logger.info(f"  Grid: {orders_placed} orders placed around ${mid_price:,.2f} (spread: {self.current_spread_bps:.1f}bps)")

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

    def _check_fills(self) -> int:
        """Check for filled orders using fetch_fills API (v20 - real exchange data).

        Queries the Paradex fills endpoint for actual fill records instead of
        guessing fills from vanished orders.
        """
        fills = 0
        try:
            # Query real fills from exchange
            fill_data = self.client.api_client.fetch_fills(params={'market': self.symbol})

            if fill_data and fill_data.get('results'):
                new_fills = []

                if self.last_fill_id is None:
                    # First check - record the latest fill ID, don't count historical
                    if fill_data['results']:
                        self.last_fill_id = fill_data['results'][0].get('id')
                        logger.info(f"  Fill tracking initialized (latest id: {self.last_fill_id})")
                else:
                    # Find fills newer than last seen
                    for f in fill_data['results']:
                        fid = f.get('id')
                        if fid == self.last_fill_id:
                            break  # Reached last seen fill
                        new_fills.append(f)

                    if new_fills:
                        self.last_fill_id = new_fills[0].get('id')

                for f in new_fills:
                    price = float(f.get('price', 0))
                    size = float(f.get('size', 0))
                    fee = float(f.get('fee', 0))
                    side = f.get('side', '?')
                    liquidity = f.get('liquidity', '?')
                    realized_pnl = float(f.get('realized_pnl', 0))

                    notional = price * size
                    self.total_volume += notional
                    self.fills_count += 1
                    self.realized_pnl += realized_pnl
                    fills += 1

                    self.last_fill_time = datetime.now()
                    self.no_fill_alert_triggered = False

                    logger.info(f"  FILL: {side} {size:.6f} @ ${price:,.2f} (${notional:,.2f}) fee=${fee:.4f} {liquidity} pnl=${realized_pnl:.4f}")

            # Sync open_orders: remove stale entries (but do NOT count as fills)
            orders = self.client.api_client.fetch_orders(params={'market': self.symbol})
            exchange_order_ids = set()
            if orders and orders.get('results'):
                for order in orders['results']:
                    if order.get('status') in ['NEW', 'OPEN', 'UNTRIGGERED']:
                        exchange_order_ids.add(order.get('id'))

            stale = [oid for oid in self.open_orders if oid not in exchange_order_ids]
            for oid in stale:
                del self.open_orders[oid]

        except Exception as e:
            logger.error(f"Check fills error: {e}")

        return fills

    def run(self):
        """Main trading loop"""
        try:
            if not self.initialize():
                return

            end_time = self.start_time + timedelta(minutes=self.duration_minutes)
            cycle = 0
            grid_reset_pct = 0.50  # v10: 0.5% price move - less whipsawing

            logger.info(f"\nStarting live trading until {end_time.strftime('%H:%M:%S')}...")
            logger.info("-" * 70)

            # Place initial grid
            self._place_grid_orders(self.grid_center)
            self.last_refresh_time = datetime.now()  # v15: Track refresh time

            while datetime.now() < end_time:
                cycle += 1

                # Get current BBO
                try:
                    bbo = self.client.api_client.fetch_bbo(market=self.symbol)
                except Exception as e:
                    logger.warning(f"BBO error: {e}")
                    time.sleep(2)
                    continue

                if not bbo:
                    time.sleep(1)
                    continue

                bid = float(bbo['bid'])
                ask = float(bbo['ask'])
                mid = (bid + ask) / 2
                spread_bps = (ask - bid) / mid * 10000

                self.price_history.append(mid)

                # Calculate ROC and update pause state
                roc = self._calculate_roc()
                self._update_pause_state(roc)

                # v17: Update tight spread mode based on low-ROC periods (NP-002)
                self._update_tight_spread_mode(roc)

                # Check for fills
                fills = self._check_fills()

                # Refresh grid on price move or fills (matches v8 paper trading logic)
                price_move_pct = abs(mid - self.grid_center) / self.grid_center * 100 if self.grid_center else 0

                # Calculate inventory ratio for force reset (use fresh balance)
                try:
                    loop_account = self.client.api_client.fetch_account_summary()
                    loop_balance = float(loop_account.account_value) if loop_account else self.capital
                except:
                    loop_balance = self.capital
                max_inventory = loop_balance * (self.max_inventory_pct / 100)
                inventory_ratio = abs(self.position_notional) / max_inventory if max_inventory > 0 else 0

                # Safety check: refresh if no tracked orders (external cancel/expire)
                no_tracked_orders = len(self.open_orders) == 0
                not_fully_paused = not (self.orders_paused and self.pause_side == 'ALL')

                # v15: Time-based refresh check (US-002)
                time_since_refresh = (datetime.now() - self.last_refresh_time).total_seconds() if self.last_refresh_time else 0
                time_based_refresh = time_since_refresh >= self.time_refresh_interval

                # v16: Check for stale orders (US-003)
                # If any tracked order is >0.2% from mid price, trigger immediate refresh
                stale_order_info = self._check_stale_orders(mid)
                stale_order_refresh = stale_order_info is not None

                should_refresh = (
                    fills > 0 or
                    price_move_pct >= grid_reset_pct or  # 0.5% price move (v10 behavior)
                    (no_tracked_orders and not_fully_paused) or  # Re-place if orders disappeared
                    time_based_refresh or  # v15: Refresh every 5 minutes regardless of price/fills
                    stale_order_refresh  # v16: Refresh if orders are stale
                )

                if should_refresh:
                    if stale_order_refresh:
                        stale_price, stale_dist = stale_order_info
                        logger.info(f"  Stale order refresh: order at ${stale_price:,.2f} is {stale_dist:.2f}% from mid")
                    elif price_move_pct >= grid_reset_pct:
                        logger.info(f"  Grid reset: price moved {price_move_pct:.3f}%")
                    elif no_tracked_orders and not_fully_paused:
                        logger.info(f"  Grid reset: no active orders, re-placing grid")
                    elif time_based_refresh:
                        logger.info(f"  Time-based refresh: {time_since_refresh:.0f}s since last placement")
                    self._sync_position()
                    self._place_grid_orders(mid, roc)
                    self.last_refresh_time = datetime.now()  # v15: Update refresh time

                # Self-learning check (every 30 min - read user notes)
                time_since_learning = (datetime.now() - self.last_self_learning_time).total_seconds()
                if time_since_learning >= self.self_learning_interval:
                    self._run_self_learning_check()

                # NP-004: Check fill rate alert
                self._check_fill_rate_alert()

                # Status log every 30 seconds
                if cycle % 30 == 0:
                    # Update balance
                    try:
                        account = self.client.api_client.fetch_account_summary()
                        self.current_balance = float(account.account_value)
                    except:
                        pass

                    pnl = self.current_balance - self.initial_balance
                    elapsed = (datetime.now() - self.start_time).total_seconds() / 60
                    inv_pct = abs(self.position_notional) / self.capital * 100 if self.capital > 0 else 0
                    pause_status = f"PAUSE-{self.pause_side}" if self.orders_paused else "LIVE"

                    # NP-004: Calculate fill rate
                    fill_rate = self._get_fill_rate_per_hour()

                    logger.info(f"\n[{elapsed:.1f}m] ${mid:,.2f} | Mkt: {spread_bps:.1f}bps | Bot: {self.current_spread_bps:.1f}bps | ROC: {roc:+.1f}bps | {pause_status}")
                    logger.info(f"  Position: {self.position_size:.6f} BTC ({inv_pct:.0f}% inv)")
                    logger.info(f"  Volume: ${self.total_volume:,.2f} | Fills: {self.fills_count} ({fill_rate:.1f}/hr)")
                    logger.info(f"  P&L: ${pnl:+.2f} (${self.current_balance:.2f})")

                    if self.total_volume > 0:
                        profit_per_10k = pnl / self.total_volume * 10000
                        logger.info(f"  Efficiency: ${profit_per_10k:+.2f} per $10k vol")

                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("\nStopping by user request...")
        except Exception as e:
            logger.error(f"Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Cleanup
            logger.info("\nCleaning up - canceling open orders...")
            self._cancel_all_orders()
            self._print_report()

    def _print_report(self):
        """Print final report"""
        # Update final balance
        try:
            account = self.client.api_client.fetch_account_summary()
            self.current_balance = float(account.account_value)
        except:
            pass

        elapsed = (datetime.now() - self.start_time).total_seconds() / 60 if self.start_time else 0
        pnl = self.current_balance - self.initial_balance

        logger.info("\n" + "=" * 70)
        logger.info("GRID MM v17 LIVE - FINAL REPORT")
        logger.info("=" * 70)
        logger.info(f"Duration: {elapsed:.1f} minutes")
        logger.info(f"Total Volume: ${self.total_volume:,.2f}")
        logger.info(f"Total Fills: {self.fills_count}")
        logger.info(f"Start Balance: ${self.initial_balance:.2f}")
        logger.info(f"End Balance: ${self.current_balance:.2f}")
        logger.info(f"P&L: ${pnl:+.2f} ({pnl/self.initial_balance*100:+.2f}%)")

        if self.total_volume > 0:
            profit_per_10k = pnl / self.total_volume * 10000
            logger.info(f"Profit per $10k volume: ${profit_per_10k:+.2f}")
            # Include rebates earned (-0.005% maker rebate)
            rebates = self.total_volume * 0.00005
            logger.info(f"Estimated rebates earned: ${rebates:.4f}")

        logger.info("=" * 70)

        # Save results
        os.makedirs('logs', exist_ok=True)
        with open('logs/grid_mm_live_results.txt', 'a') as f:
            f.write(f"\n--- {datetime.now()} ---\n")
            f.write(f"Duration: {elapsed:.1f} min\n")
            f.write(f"Volume: ${self.total_volume:,.2f}\n")
            f.write(f"Fills: {self.fills_count}\n")
            f.write(f"P&L: ${pnl:+.2f}\n")
            if self.total_volume > 0:
                f.write(f"Per $10k: ${profit_per_10k:+.2f}\n")


def main():
    """
    LIVE Grid MM v12 for Paradex account

    Dynamic spread based on ROC volatility:
    - ROC 0-5 bps → 1.5 bps spread (calm market)
    - ROC 5-15 bps → 3 bps spread (low volatility)
    - ROC 15-30 bps → 6 bps spread (moderate volatility)
    - ROC 30-50 bps → 10 bps spread (high volatility)
    - ROC >50 bps → PAUSE orders
    """
    mm = GridMarketMakerLive(
        symbol="BTC-USD-PERP",
        base_spread_bps=1.5,        # v19: tight calm spread for fills
        order_size_usd=40.0,        # $40 per order (must be < balance for inventory check)
        num_levels=2,               # 2 levels per side
        duration_minutes=525600,    # Run indefinitely (1 year)
        max_inventory_pct=200.0,    # Allow leveraged positions
        capital=91.0,               # ~$91 account balance
        roc_threshold_bps=50.0,     # v10: real trends only per Qwen
        min_pause_duration=300,     # v10: 5 min pause per Qwen
    )
    mm.run()


if __name__ == "__main__":
    main()
