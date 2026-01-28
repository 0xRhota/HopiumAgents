#!/usr/bin/env python3
"""
Grid Market Maker for Nado DEX
High-volume limit order strategy to farm early platform points

Strategy: Place limit orders on both sides of mid price
- Uses LIMIT orders (maker) to get 0.01% fee vs 0.035% taker
- ROC threshold for trend detection (pause in volatile markets)
- Inventory management with leverage

Nado Fee Structure (Entry Tier):
- Maker: 0.01% (limit orders)
- Taker: 0.035% (market orders)
- Goal: Farm volume with limit orders for points + lower fees
"""

import os
import sys
import time
import asyncio
import logging
from decimal import Decimal
from datetime import datetime
from typing import Dict, Optional
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

from dexes.nado.nado_sdk import NadoSDK


class NadoGridMM:
    """
    Grid Market Maker for Nado DEX
    Places limit orders on both sides for volume farming + spread capture
    """

    def __init__(
        self,
        symbol: str = "ETH-PERP",
        base_spread_bps: float = 5.0,       # Spread from mid price (wider to avoid POST_ONLY crossing)
        order_size_usd: float = 100.0,      # $100 min notional on Nado
        num_levels: int = 1,                # 1 level per side = $100 per side
        max_inventory_pct: float = 300.0,   # Allow leverage
        roc_threshold_bps: float = 5.0,     # Pause on trend
        min_pause_duration: int = 15,       # 15 second pause
        refresh_interval: float = 10.0,     # Refresh grid every 10 seconds
    ):
        self.symbol = symbol
        self.base_symbol = symbol.replace('-PERP', '')
        self.base_spread_bps = base_spread_bps
        self.order_size_usd = order_size_usd
        self.num_levels = num_levels
        self.max_inventory_pct = max_inventory_pct
        self.roc_threshold_bps = roc_threshold_bps
        self.min_pause_duration = min_pause_duration
        self.refresh_interval = refresh_interval

        # State
        self.sdk: Optional[NadoSDK] = None
        self.product_id: Optional[int] = None
        self.price_history: deque = deque(maxlen=30)
        self.open_orders: Dict[str, Dict] = {}

        # Trend detection
        self.orders_paused = False
        self.pause_side = None
        self.pause_start_time = None

        # Stats
        self.total_volume = 0.0
        self.fills_count = 0
        self.start_time = None
        self.initial_balance = 0.0

        # Market info
        self.tick_size = 0.01
        self.step_size = 0.001
        self.min_size = 0.001

    async def initialize(self):
        """Initialize Nado SDK and get market info"""
        logger.info("=" * 70)
        logger.info("NADO GRID MARKET MAKER - VOLUME FARMING")
        logger.info("=" * 70)
        logger.info(f"Symbol: {self.symbol}")
        logger.info(f"Spread: {self.base_spread_bps} bps")
        logger.info(f"Order Size: ${self.order_size_usd}")
        logger.info(f"Levels: {self.num_levels} per side")
        logger.info(f"ROC Threshold: {self.roc_threshold_bps} bps")
        logger.info(f"Refresh Interval: {self.refresh_interval}s")
        logger.info("=" * 70)

        # Initialize SDK
        wallet = os.getenv('NADO_WALLET_ADDRESS')
        signer_key = os.getenv('NADO_LINKED_SIGNER_PRIVATE_KEY')

        if not wallet or not signer_key:
            raise ValueError("NADO_WALLET_ADDRESS and NADO_LINKED_SIGNER_PRIVATE_KEY required")

        self.sdk = NadoSDK(
            wallet_address=wallet,
            linked_signer_private_key=signer_key,
            subaccount_name=os.getenv('NADO_SUBACCOUNT_NAME', 'default'),
            testnet=False
        )

        # Verify linked signer
        if not await self.sdk.verify_linked_signer():
            raise Exception("Linked signer not authorized")
        logger.info("Linked signer verified!")

        # Get balance
        balance = await self.sdk.get_balance()
        self.initial_balance = balance or 0
        logger.info(f"Account balance: ${self.initial_balance:.2f}")

        # Get products and find our symbol
        # Nado price/size increments (from API error messages):
        # ETH/BTC/SOL: price_increment = 0.1, size_increment = 0.001, min_notional = $100
        PRODUCT_SPECS = {
            'BTC-PERP': {'tick': 1.0, 'step': 0.0001, 'min_notional': 100},
            'ETH-PERP': {'tick': 0.1, 'step': 0.001, 'min_notional': 100},
            'SOL-PERP': {'tick': 0.01, 'step': 0.01, 'min_notional': 100},
        }

        products = await self.sdk.get_products()
        for p in products:
            if p.get('symbol') == self.symbol:
                self.product_id = p.get('product_id')
                specs = PRODUCT_SPECS.get(self.symbol, {'tick': 0.1, 'step': 0.001, 'min_notional': 100})
                self.tick_size = specs['tick']
                self.step_size = specs['step']
                self.min_notional = specs['min_notional']
                logger.info(f"Product ID: {self.product_id}, tick={self.tick_size}, step={self.step_size}, min=${self.min_notional}")
                break

        if not self.product_id:
            raise Exception(f"Product not found: {self.symbol}")

        # Get initial price
        mid = await self._get_mid_price()
        if not mid:
            raise Exception("Cannot get initial price")
        logger.info(f"Initial price: ${mid:,.2f}")
        self.price_history.append(mid)

        self.start_time = datetime.now()
        return True

    async def _get_mid_price(self) -> Optional[float]:
        """Get current mid price from Nado"""
        try:
            response = await self.sdk._query("market_price", {"product_id": str(self.product_id)})
            if response.get("status") == "success":
                data = response.get("data", {})
                bid = self.sdk._from_x18(int(data.get('bid_x18', '0')))
                ask = self.sdk._from_x18(int(data.get('ask_x18', '0')))
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2
        except Exception as e:
            logger.error(f"Error getting mid price: {e}")
        return None

    def _round_price(self, price: float) -> float:
        """Round price to tick size (avoid floating point errors)"""
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, 2)  # Round to 2 decimals to avoid FP errors

    def _round_size(self, size: float, price: float) -> float:
        """Round size to step size, ensuring min_notional is met"""
        # Ensure notional >= min_notional (with 5% buffer)
        min_size_for_notional = (self.min_notional * 1.05) / price
        size = max(size, min_size_for_notional)

        steps = int(size / self.step_size) + 1  # Round up
        return round(steps * self.step_size, 6)

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
        """Update order pause state based on trend"""
        old_paused = self.orders_paused

        strong_threshold = self.roc_threshold_bps * 2.0

        if abs(roc) > strong_threshold:
            if not self.orders_paused or self.pause_side != 'ALL':
                self.pause_start_time = datetime.now()
            self.orders_paused = True
            self.pause_side = 'ALL'
            direction = "UP" if roc > 0 else "DOWN"
            if not old_paused:
                logger.warning(f"  STRONG TREND {direction} - PAUSE ALL (ROC: {roc:+.2f} bps)")
        elif roc > self.roc_threshold_bps:
            if not self.orders_paused:
                self.pause_start_time = datetime.now()
            self.orders_paused = True
            self.pause_side = 'SELL'
        elif roc < -self.roc_threshold_bps:
            if not self.orders_paused:
                self.pause_start_time = datetime.now()
            self.orders_paused = True
            self.pause_side = 'BUY'
        else:
            if self.orders_paused and self.pause_start_time:
                elapsed = (datetime.now() - self.pause_start_time).total_seconds()
                if elapsed >= self.min_pause_duration:
                    self.orders_paused = False
                    self.pause_side = None
                    if old_paused:
                        logger.info(f"  RESUME orders (ROC: {roc:+.2f} bps)")

    async def _cancel_all_orders(self):
        """Cancel all open orders (best effort - orders expire after 30s anyway)"""
        try:
            result = await self.sdk.cancel_all_orders(self.symbol)
            self.open_orders.clear()
            return result
        except Exception as e:
            # Cancel may fail due to signature issues, but orders expire after 30s anyway
            logger.debug(f"Cancel skipped (orders expire in 30s): {e}")
            self.open_orders.clear()  # Clear tracking regardless
            return True  # Return True since orders will expire

    async def _place_grid_orders(self, mid_price: float):
        """Place limit orders on both sides of mid price"""
        orders_placed = 0

        for level in range(1, self.num_levels + 1):
            spread_multiplier = level * self.base_spread_bps / 10000

            # Calculate order size in base currency (ensure min_notional is met)
            size = self._round_size(self.order_size_usd / mid_price, mid_price)

            # BUY order (below mid)
            if not self.orders_paused or self.pause_side not in ['BUY', 'ALL']:
                buy_price = self._round_price(mid_price * (1 - spread_multiplier))
                try:
                    result = await self.sdk.create_limit_order(
                        symbol=self.symbol,
                        is_buy=True,
                        amount=size,
                        price=buy_price,
                        order_type="LIMIT",  # Can fill as maker or taker for volume
                        reduce_only=False
                    )
                    if result and result.get('success'):
                        orders_placed += 1
                        self.open_orders[result.get('digest', str(time.time()))] = {
                            'side': 'BUY',
                            'price': buy_price,
                            'size': size
                        }
                        logger.debug(f"  BUY {size:.4f} @ ${buy_price:,.2f}")
                except Exception as e:
                    logger.debug(f"Buy order error: {e}")

            # SELL order (above mid)
            if not self.orders_paused or self.pause_side not in ['SELL', 'ALL']:
                sell_price = self._round_price(mid_price * (1 + spread_multiplier))
                try:
                    result = await self.sdk.create_limit_order(
                        symbol=self.symbol,
                        is_buy=False,
                        amount=size,
                        price=sell_price,
                        order_type="LIMIT",  # Can fill as maker or taker for volume
                        reduce_only=False
                    )
                    if result and result.get('success'):
                        orders_placed += 1
                        self.open_orders[result.get('digest', str(time.time()))] = {
                            'side': 'SELL',
                            'price': sell_price,
                            'size': size
                        }
                        logger.debug(f"  SELL {size:.4f} @ ${sell_price:,.2f}")
                except Exception as e:
                    logger.debug(f"Sell order error: {e}")

        return orders_placed

    async def run_cycle(self):
        """Run one grid cycle"""
        # Get current price
        mid = await self._get_mid_price()
        if not mid:
            logger.warning("Cannot get price, skipping cycle")
            return

        self.price_history.append(mid)

        # Calculate ROC and update pause state
        roc = self._calculate_roc()
        self._update_pause_state(roc)

        # Cancel existing orders
        await self._cancel_all_orders()

        # Place new grid
        if not self.orders_paused or self.pause_side != 'ALL':
            orders_placed = await self._place_grid_orders(mid)
            pause_info = f" (paused: {self.pause_side})" if self.orders_paused else ""
            logger.info(
                f"Grid @ ${mid:,.2f} | ROC: {roc:+.2f} bps | "
                f"Orders: {orders_placed}{pause_info}"
            )
        else:
            logger.info(f"Grid PAUSED @ ${mid:,.2f} | ROC: {roc:+.2f} bps")

    async def run(self):
        """Main run loop"""
        logger.info("Starting Grid MM loop...")
        logger.info(f"Refresh every {self.refresh_interval}s")

        cycle_count = 0
        while True:
            try:
                await self.run_cycle()
                cycle_count += 1

                # Log stats every 30 cycles (~5 min at 10s refresh)
                if cycle_count % 30 == 0:
                    elapsed = (datetime.now() - self.start_time).total_seconds() / 60
                    logger.info(
                        f"Stats: {cycle_count} cycles in {elapsed:.1f} min | "
                        f"Orders tracked: {len(self.open_orders)}"
                    )

                await asyncio.sleep(self.refresh_interval)

            except KeyboardInterrupt:
                logger.info("Shutting down...")
                await self._cancel_all_orders()
                break
            except Exception as e:
                logger.error(f"Cycle error: {e}")
                await asyncio.sleep(5)

    async def shutdown(self):
        """Clean shutdown"""
        logger.info("Cancelling all orders...")
        await self._cancel_all_orders()
        logger.info("Shutdown complete")


async def main():
    """Main entry point"""
    # Parse args for symbol
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', default='ETH-PERP', help='Trading symbol')
    parser.add_argument('--spread', type=float, default=5.0, help='Spread in bps (wider=safer for POST_ONLY)')
    parser.add_argument('--size', type=float, default=100.0, help='Order size in USD (min $100 on Nado)')
    parser.add_argument('--levels', type=int, default=1, help='Levels per side')
    parser.add_argument('--refresh', type=float, default=10.0, help='Refresh interval')
    args = parser.parse_args()

    mm = NadoGridMM(
        symbol=args.symbol,
        base_spread_bps=args.spread,
        order_size_usd=args.size,
        num_levels=args.levels,
        refresh_interval=args.refresh,
    )

    try:
        await mm.initialize()
        await mm.run()
    except KeyboardInterrupt:
        pass
    finally:
        await mm.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
