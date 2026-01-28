#!/usr/bin/env python3
"""
Grid Market Maker v8 - Hibachi DEX
EXACT mirror of Paradex grid_mm_live.py strategy

Strategy: Place limit orders on both sides of mid price
- Grid resets on 0.25% price move (NOT time-based)
- Inventory skew at 70% threshold
- ROC-based trend pause (3.0 bps threshold, 15s pause)

v8 Parameters (from Paradex):
- Spread: 1.5 bps
- ROC threshold: 3.0 bps
- Pause duration: 15 seconds
- Inventory limit: 200% (leverage)
- Grid reset: 0.25% price move
"""

import os
import sys
import time
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional
from collections import deque

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

from dexes.hibachi.hibachi_sdk import HibachiSDK


class GridMarketMakerHibachi:
    """
    Grid Market Maker v8 for Hibachi - EXACT mirror of Paradex strategy
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT-P",
        base_spread_bps: float = 7.0,      # v9: Wider spread for volatility
        order_size_usd: float = 100.0,     # $100 per order
        num_levels: int = 2,               # 2 levels per side
        max_inventory_pct: float = 100.0,  # v9: Lower leverage limit
        capital: float = 40.0,             # Account capital
        hedge_symbol: str = "ETH/USDT-P",  # Cross-asset LONG hedge
        hedge_size_pct: float = 80.0,      # Use 80% of capital for hedge
        roc_threshold_bps: float = 3.0,    # Trend detection
        min_pause_duration: int = 15,      # v8: 15 second pause
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
        self.sdk: Optional[HibachiSDK] = None
        self.grid_center = None
        self.open_orders: Dict[str, Dict] = {}
        self.price_history: deque = deque(maxlen=30)

        # Trend detection
        self.orders_paused = False
        self.pause_side = None
        self.pause_start_time = None

        # Stats
        self.total_volume = 0.0
        self.fills_count = 0
        self.start_time = None
        self.initial_balance = 0.0

        # Position tracking
        self.position_size = 0.0
        self.position_notional = 0.0

        # Market info
        self.tick_size = 0.1
        self.step_size = 0.0001
        self.min_notional = 1.0

        # Grid reset threshold (v8: 0.25% price move)
        self.grid_reset_pct = 0.25

    async def initialize(self):
        """Initialize Hibachi SDK"""
        logger.info("=" * 70)
        logger.info("HIBACHI GRID MM v8 - EXACT PARADEX MIRROR")
        logger.info("=" * 70)
        logger.info(f"Symbol: {self.symbol}")
        logger.info(f"Spread: {self.base_spread_bps} bps")
        logger.info(f"Order Size: ${self.order_size_usd}")
        logger.info(f"Levels: {self.num_levels} per side")
        logger.info(f"Grid Reset: {self.grid_reset_pct}% price move")
        logger.info(f"ROC Threshold: {self.roc_threshold_bps} bps")
        logger.info(f"Min Pause: {self.min_pause_duration}s")
        logger.info("=" * 70)

        # Initialize SDK
        api_key = os.getenv('HIBACHI_PUBLIC_KEY')
        api_secret = os.getenv('HIBACHI_PRIVATE_KEY')
        account_id = os.getenv('HIBACHI_ACCOUNT_ID')

        if not api_key or not api_secret or not account_id:
            raise ValueError("HIBACHI credentials required in .env")

        self.sdk = HibachiSDK(api_key, api_secret, account_id)

        # Get balance
        balance = await self.sdk.get_balance()
        self.initial_balance = balance or 0
        self.capital = min(self.capital, self.initial_balance)
        logger.info(f"Account balance: ${self.initial_balance:.2f}")

        # Get market info
        market_info = await self.sdk.get_market_info(self.symbol)
        if market_info:
            self.tick_size = float(market_info.get('tickSize', 0.1))
            self.step_size = float(market_info.get('minOrderSize', 0.0001))
            self.min_notional = float(market_info.get('minNotional', 1.0))
            logger.info(f"Market: tick={self.tick_size}, step={self.step_size}, min=${self.min_notional}")

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

        # Open hedge LONG on cross-asset (ETH when grid is BTC)
        await self._open_hedge_long()

        return True

    async def _get_mid_price(self) -> Optional[float]:
        """Get current mid price"""
        try:
            orderbook = await self.sdk.get_orderbook(self.symbol)
            if not orderbook:
                return None

            bid_levels = orderbook.get('bid', {}).get('levels', [])
            ask_levels = orderbook.get('ask', {}).get('levels', [])

            if not bid_levels or not ask_levels:
                return None

            best_bid = float(bid_levels[0]['price'])
            best_ask = float(ask_levels[0]['price'])
            return (best_bid + best_ask) / 2
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

                    # Close any SHORT position on grid symbol (bullish setup)
                    if self.position_size < 0:
                        logger.info(f"🔄 Closing SHORT {self.symbol} for bullish setup...")
                        result = await self.sdk.create_market_order(
                            symbol=self.symbol,
                            is_buy=True,
                            amount=abs(self.position_size)
                        )
                        if result:
                            logger.info(f"  ✅ Closed SHORT {self.symbol}")
                            self.position_size = 0
                            self.position_notional = 0
                        await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Position sync error: {e}")

    async def _open_hedge_long(self):
        """Open a LONG position on the hedge asset (ETH when grid is BTC)"""
        if self.hedge_position_opened:
            return

        try:
            # Check current hedge position
            hedge_size = await self.sdk.get_position_size(self.hedge_symbol)
            if hedge_size and hedge_size > 0:
                logger.info(f"✅ Hedge already LONG: {self.hedge_symbol} = {hedge_size:.6f}")
                self.hedge_position_opened = True
                return

            # If SHORT, close it first
            if hedge_size and hedge_size < 0:
                logger.info(f"🔄 Closing SHORT hedge on {self.hedge_symbol}...")
                result = await self.sdk.create_market_order(
                    symbol=self.hedge_symbol,
                    is_buy=True,
                    amount=abs(hedge_size)
                )
                if result:
                    logger.info(f"  ✅ Closed SHORT {self.hedge_symbol}")
                await asyncio.sleep(1)

            # Calculate hedge size (% of capital)
            hedge_notional = self.capital * (self.hedge_size_pct / 100)

            # Get hedge asset price
            orderbook = await self.sdk.get_orderbook(self.hedge_symbol)
            if not orderbook:
                logger.error("Cannot get hedge orderbook")
                return

            bid_levels = orderbook.get('bid', {}).get('levels', [])
            ask_levels = orderbook.get('ask', {}).get('levels', [])
            if not bid_levels or not ask_levels:
                logger.error("Empty hedge orderbook")
                return

            hedge_price = (float(bid_levels[0]['price']) + float(ask_levels[0]['price'])) / 2

            # Calculate size
            hedge_amount = hedge_notional / hedge_price
            hedge_amount = self._round_size(hedge_amount)

            logger.info(f"🚀 Opening LONG hedge: {self.hedge_symbol} ${hedge_notional:.2f} @ ${hedge_price:,.2f}")

            result = await self.sdk.create_market_order(
                symbol=self.hedge_symbol,
                is_buy=True,
                amount=hedge_amount
            )

            if result:
                logger.info(f"  ✅ HEDGE LONG opened: {self.hedge_symbol} {hedge_amount:.6f} (${hedge_notional:.2f})")
                self.hedge_position_opened = True
            else:
                logger.error(f"  ❌ Hedge order failed")

        except Exception as e:
            logger.error(f"Hedge open error: {e}")

    def _round_price(self, price: float) -> float:
        """Round price to tick size"""
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, 6)

    def _round_size(self, size: float) -> float:
        """Round size to step size"""
        steps = int(size / self.step_size)
        return max(steps * self.step_size, self.step_size)

    def _calculate_roc(self) -> float:
        """Calculate Rate of Change in bps"""
        if len(self.price_history) < 10:
            return 0.0
        prices = list(self.price_history)
        current = prices[-1]
        past = prices[-10]
        if past == 0:
            return 0.0
        return (current - past) / past * 10000

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
            cancelled = await self.sdk.cancel_all_orders(self.symbol)
            self.open_orders.clear()
            if cancelled > 0:
                logger.info(f"  Cancelled {cancelled} orders")
        except Exception as e:
            logger.error(f"Cancel error: {e}")
            self.open_orders.clear()

    async def _place_grid_orders(self, mid_price: float):
        """Place grid orders - EXACT v8 inventory skew logic"""
        # First cancel existing orders
        await self._cancel_all_orders()

        # If ALL orders paused, don't place anything
        if self.orders_paused and self.pause_side == 'ALL':
            logger.info(f"  Grid SKIPPED: Strong trend, all orders paused")
            return

        spread_pct = self.base_spread_bps / 10000
        max_inventory = self.capital * (self.max_inventory_pct / 100)

        # Inventory ratio (signed: positive=long, negative=short)
        inv_ratio = self.position_notional / max_inventory if max_inventory > 0 else 0

        # v8 INVENTORY SKEW LOGIC
        buy_mult = 1.0
        sell_mult = 1.0
        min_mult = self.min_notional / self.order_size_usd if self.order_size_usd > 0 else 1.0

        if inv_ratio > 0.7:  # Heavy LONG - ONLY sells
            buy_mult = 0.0
            sell_mult = 1.5
            logger.info(f"  REDUCE LONG MODE: {inv_ratio*100:.0f}% - sells only")
        elif inv_ratio > 0.3:
            buy_mult = max(min_mult, 0.3)
            sell_mult = 1.3
        elif inv_ratio < -0.7:  # Heavy SHORT - ONLY buys
            buy_mult = 1.5
            sell_mult = 0.0
            logger.info(f"  REDUCE SHORT MODE: {inv_ratio*100:.0f}% - buys only")
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
                    continue

                try:
                    result = await self.sdk.create_limit_order(
                        symbol=self.symbol,
                        is_buy=True,
                        amount=size,
                        price=price
                    )
                    if result and 'orderId' in result:
                        self.open_orders[result['orderId']] = {
                            'side': 'BUY', 'price': price, 'size': size
                        }
                        orders_placed += 1
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
                        price=price
                    )
                    if result and 'orderId' in result:
                        self.open_orders[result['orderId']] = {
                            'side': 'SELL', 'price': price, 'size': size
                        }
                        orders_placed += 1
                except Exception as e:
                    logger.debug(f"SELL L{i} error: {e}")

        self.grid_center = mid_price
        if orders_placed > 0:
            logger.info(f"  Grid: {orders_placed} orders @ ${mid_price:,.2f}")

    async def _check_fills(self) -> int:
        """Check for filled orders - EXACT v8 logic"""
        fills = 0
        try:
            orders = await self.sdk.get_orders(self.symbol)
            exchange_order_ids = set()

            for order in orders:
                order_id = order.get('orderId')
                status = order.get('status')

                if status in ['OPEN', 'PLACED', 'NEW']:
                    exchange_order_ids.add(order_id)

                if order_id in self.open_orders and status in ['FILLED', 'CLOSED']:
                    info = self.open_orders[order_id]
                    filled_size = float(order.get('filledSize', info['size']))
                    fill_price = float(order.get('avgFillPrice', info['price']))

                    notional = filled_size * fill_price
                    self.total_volume += notional
                    self.fills_count += 1
                    fills += 1

                    logger.info(f"  FILL: {info['side']} {filled_size:.6f} @ ${fill_price:,.2f} (${notional:,.2f})")
                    del self.open_orders[order_id]

            # Remove stale tracked orders
            stale = [oid for oid in self.open_orders if oid not in exchange_order_ids]
            for oid in stale:
                del self.open_orders[oid]

        except Exception as e:
            logger.error(f"Check fills error: {e}")

        return fills

    async def run(self):
        """Main trading loop - EXACT v8 logic with price-based reset"""
        try:
            if not await self.initialize():
                return

            logger.info(f"\nStarting grid MM (reset on {self.grid_reset_pct}% move)...")
            logger.info("-" * 70)

            # Place initial grid
            await self._place_grid_orders(self.grid_center)

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

                # Check for fills
                fills = await self._check_fills()

                # v8: Price-based grid reset (0.25% move)
                price_move_pct = abs(mid - self.grid_center) / self.grid_center * 100 if self.grid_center else 0

                # Inventory ratio for force reset
                max_inventory = self.capital * (self.max_inventory_pct / 100)
                inventory_ratio = abs(self.position_notional) / max_inventory if max_inventory > 0 else 0

                # Safety: refresh if no tracked orders
                no_tracked = len(self.open_orders) == 0
                not_paused = not (self.orders_paused and self.pause_side == 'ALL')

                should_refresh = (
                    fills > 0 or
                    price_move_pct >= self.grid_reset_pct or
                    inventory_ratio > 0.8 or
                    (no_tracked and not_paused)
                )

                if should_refresh:
                    if price_move_pct >= self.grid_reset_pct:
                        logger.info(f"  Grid reset: price moved {price_move_pct:.3f}%")
                    elif inventory_ratio > 0.8:
                        logger.info(f"  Grid reset: inventory at {inventory_ratio*100:.0f}%")
                    elif no_tracked and not_paused:
                        logger.info(f"  Grid reset: no active orders")
                    await self._sync_position()
                    await self._place_grid_orders(mid)

                # Status log every 30 cycles
                if cycle % 30 == 0:
                    balance = await self.sdk.get_balance() or 0
                    pnl = balance - self.initial_balance
                    elapsed = (datetime.now() - self.start_time).total_seconds() / 60
                    inv_pct = abs(self.position_notional) / self.capital * 100 if self.capital > 0 else 0
                    pause_status = f"PAUSE-{self.pause_side}" if self.orders_paused else "LIVE"

                    logger.info(f"\n[{elapsed:.1f}m] ${mid:,.2f} | ROC: {roc:+.1f}bps | {pause_status}")
                    logger.info(f"  Position: {self.position_size:.6f} ({inv_pct:.0f}% inv)")
                    logger.info(f"  Volume: ${self.total_volume:,.2f} | Fills: {self.fills_count}")
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
        logger.info("HIBACHI GRID MM v8 - FINAL REPORT")
        logger.info("=" * 70)
        logger.info(f"Duration: {elapsed:.1f} minutes")
        logger.info(f"Total Volume: ${self.total_volume:,.2f}")
        logger.info(f"Total Fills: {self.fills_count}")
        logger.info("=" * 70)


async def main():
    mm = GridMarketMakerHibachi(
        symbol="BTC/USDT-P",
        base_spread_bps=1.5,         # v8: tight spread
        order_size_usd=100.0,        # $100 per order
        num_levels=2,                # 2 levels per side
        max_inventory_pct=200.0,     # Allow leverage
        capital=60.0,
        roc_threshold_bps=3.0,       # v8 threshold
        min_pause_duration=15,       # v8: 15s pause
    )
    await mm.run()


if __name__ == "__main__":
    asyncio.run(main())
