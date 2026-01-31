#!/usr/bin/env python3
"""
Grid Market Maker v18 - Extended DEX (Starknet)
Qwen-calibrated dynamic spread, POST_ONLY maker-only orders.

REQUIRES Python 3.11+ (x10-python-trading-starknet)

Strategy: Place POST_ONLY limit orders on both sides of mid price.
- Dynamic spread adjusts with ROC volatility (4-15 bps)
- POST_ONLY ensures all fills are maker (zero/low fees)
- Refresh on 5-min timer or 0.5% price move
- ROC > 50 bps pauses all orders

v18 Parameters (Qwen-calibrated):
- Spread: DYNAMIC based on ROC
  - ROC 0-5 bps → 4 bps spread (calm market)
  - ROC 5-10 bps → 6 bps spread (low volatility)
  - ROC 10-20 bps → 8 bps spread (moderate volatility)
  - ROC 20-30 bps → 12 bps spread (high volatility)
  - ROC 30-50 bps → 15 bps spread (very high volatility)
  - ROC >50 bps → PAUSE orders (trend detected)
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections import deque
from typing import Optional

from dotenv import load_dotenv
load_dotenv('.env')

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Market precision config
MARKET_CONFIG = {
    "BTC-USD": {"amount_precision": 5, "price_precision": 0, "min_size_usd": 10},
    "ETH-USD": {"amount_precision": 3, "price_precision": 0, "min_size_usd": 10},
    "SOL-USD": {"amount_precision": 2, "price_precision": 2, "min_size_usd": 10},
    "XCU-USD": {"amount_precision": 1, "price_precision": 4, "min_size_usd": 6},  # New listing - points boost
}


class GridMarketMakerExtended:
    """
    Grid Market Maker for Extended DEX using POST_ONLY orders.
    """

    def __init__(
        self,
        symbol: str = "BTC-USD",
        order_size_usd: float = 50.0,
        num_levels: int = 2,
        max_inventory_pct: float = 100.0,
        roc_threshold_bps: float = 50.0,
        min_pause_duration: int = 120,
        time_refresh_interval: int = 300,
        grid_reset_pct: float = 0.5,
    ):
        self.symbol = symbol
        self.order_size_usd = order_size_usd
        self.num_levels = num_levels
        self.max_inventory_pct = max_inventory_pct
        self.roc_threshold_bps = roc_threshold_bps
        self.min_pause_duration = min_pause_duration
        self.time_refresh_interval = time_refresh_interval
        self.grid_reset_pct = grid_reset_pct

        # State
        self.client = None
        self.grid_center = None
        self.current_spread_bps = 4.0
        self.last_spread_bps = 4.0
        self.is_paused = False
        self.pause_start_time = None
        self.last_refresh_time = None

        # Price tracking for ROC
        self.price_history = deque(maxlen=180)  # 3-minute window at 1s intervals
        self.roc_window = 180

        # Stats
        self.session_start = datetime.now()
        self.total_orders_placed = 0
        self.total_fills = 0
        self.total_volume = 0.0
        self.open_order_ids = []

        # v20: Real fill tracking via get_trades API (replaces broken vanished-order detection)
        self.last_trade_id = None

        # Market config
        config = MARKET_CONFIG.get(symbol, MARKET_CONFIG["BTC-USD"])
        self.amount_precision = config["amount_precision"]
        self.price_precision = config["price_precision"]

    async def initialize(self):
        """Initialize Extended SDK (x10 PerpetualTradingClient)"""
        logger.info("=" * 70)
        logger.info("EXTENDED GRID MM v18 - MAKER-ONLY (POST_ONLY)")
        logger.info("=" * 70)
        logger.info(f"Symbol: {self.symbol}")
        logger.info(f"Spread: DYNAMIC (4-15 bps, Qwen-calibrated)")
        logger.info(f"  ROC 0-5 bps → 4 bps spread (calm)")
        logger.info(f"  ROC 5-10 bps → 6 bps spread (low vol)")
        logger.info(f"  ROC 10-20 bps → 8 bps spread (moderate)")
        logger.info(f"  ROC 20-30 bps → 12 bps spread (high vol)")
        logger.info(f"  ROC 30-50 bps → 15 bps spread (very high vol)")
        logger.info(f"  ROC >50 bps → PAUSE orders")
        logger.info(f"Order Type: POST_ONLY (maker-only)")
        logger.info(f"Order Size: ${self.order_size_usd}")
        logger.info(f"Levels: {self.num_levels} per side")
        logger.info(f"Time Refresh: {self.time_refresh_interval}s")
        logger.info("=" * 70)

        # Create x10 trading client
        from x10.perpetual.accounts import StarkPerpetualAccount
        from x10.perpetual.configuration import MAINNET_CONFIG
        from x10.perpetual.trading_client import PerpetualTradingClient

        api_key = os.getenv("EXTENDED") or os.getenv("EXTENDED_API_KEY")
        private_key = os.getenv("EXTENDED_STARK_PRIVATE_KEY")
        public_key = os.getenv("EXTENDED_STARK_PUBLIC_KEY")
        vault = os.getenv("EXTENDED_VAULT")

        if not all([api_key, private_key, public_key, vault]):
            raise ValueError(
                "Missing Extended credentials. Set in .env:\n"
                "  EXTENDED_API_KEY, EXTENDED_STARK_PRIVATE_KEY,\n"
                "  EXTENDED_STARK_PUBLIC_KEY, EXTENDED_VAULT"
            )

        stark_account = StarkPerpetualAccount(
            vault=int(vault),
            private_key=private_key,
            public_key=public_key,
            api_key=api_key,
        )

        self.client = PerpetualTradingClient(
            endpoint_config=MAINNET_CONFIG,
            stark_account=stark_account
        )

        # Verify connection
        balance = await self.client.account.get_balance()
        if balance and balance.data:
            equity = float(balance.data.equity)
            logger.info(f"Account equity: ${equity:.2f}")
        else:
            raise ValueError("Failed to fetch account balance")

        # Check fees
        try:
            fees = await self.client.account.get_fees(market_names=[self.symbol])
            if fees and fees.data:
                fee = fees.data[0]
                maker_pct = float(fee.maker_fee_rate) * 100
                taker_pct = float(fee.taker_fee_rate) * 100
                logger.info(f"Fees - Maker: {maker_pct:.3f}%, Taker: {taker_pct:.3f}%")
        except Exception as e:
            logger.warning(f"Could not fetch fees: {e}")

        # Get initial price
        mid = await self._get_mid_price()
        if mid:
            logger.info(f"Initial price: ${mid:,.2f}")
            self.grid_center = mid
        else:
            raise ValueError(f"Failed to get price for {self.symbol}")

        # Get current position
        pos = await self._get_position()
        if pos:
            logger.info(f"Position: {pos['side']} {abs(pos['size']):.6f} (${abs(pos['notional']):.2f})")
        else:
            logger.info("Position: FLAT")

    async def _get_mid_price(self) -> Optional[float]:
        """Get mid price from orderbook snapshot."""
        try:
            ob = await self.client.markets_info.get_orderbook_snapshot(
                market_name=self.symbol
            )
            if ob and ob.data:
                bid_list = ob.data.bid
                ask_list = ob.data.ask
                if bid_list and ask_list:
                    best_bid = float(bid_list[0].price)
                    best_ask = float(ask_list[0].price)
                    return (best_bid + best_ask) / 2
            # Fallback to market stats
            stats = await self.client.markets_info.get_market_statistics(
                market_name=self.symbol
            )
            if stats and stats.data:
                return float(stats.data.last_price)
        except Exception as e:
            logger.error(f"Error getting mid price: {e}")
        return None

    async def _get_position(self) -> Optional[dict]:
        """Get current position for symbol."""
        try:
            positions = await self.client.account.get_positions(
                market_names=[self.symbol]
            )
            if positions and positions.data:
                for pos in positions.data:
                    size = float(pos.size) if pos.size else 0
                    if abs(size) > 0:
                        entry = float(pos.entry_price) if pos.entry_price else 0
                        mid = await self._get_mid_price() or entry
                        return {
                            "side": "LONG" if size > 0 else "SHORT",
                            "size": size,
                            "notional": abs(size) * mid,
                            "entry_price": entry,
                        }
        except Exception as e:
            logger.error(f"Error getting position: {e}")
        return None

    async def _get_balance(self) -> float:
        """Get account equity."""
        try:
            balance = await self.client.account.get_balance()
            if balance and balance.data:
                return float(balance.data.equity)
        except Exception as e:
            logger.error(f"Error getting balance: {e}")
        return 0.0

    async def _cancel_all_orders(self):
        """Cancel all open orders for this symbol."""
        try:
            result = await self.client.orders.mass_cancel(
                markets=[self.symbol]
            )
            self.open_order_ids = []
            logger.info("  Orders cancelled")
        except Exception as e:
            logger.error(f"Error cancelling orders: {e}")

    async def _place_grid_orders(self, mid: float):
        """Place POST_ONLY grid orders around mid price."""
        from x10.perpetual.orders import OrderSide, TimeInForce

        spread_decimal = self.current_spread_bps / 10000.0
        orders_placed = 0
        balance = await self._get_balance()
        pos = await self._get_position()

        # Calculate inventory ratio
        pos_notional = abs(pos["notional"]) if pos else 0
        max_notional = balance * (self.max_inventory_pct / 100.0)
        inventory_ratio = pos_notional / max_notional if max_notional > 0 else 0

        # Determine which sides to quote
        can_buy = True
        can_sell = True
        if pos:
            if pos["side"] == "LONG" and inventory_ratio > 0.8:
                can_buy = False  # Too long, only sell
            elif pos["side"] == "SHORT" and inventory_ratio > 0.8:
                can_sell = False  # Too short, only buy

        # Place BUY orders (below mid)
        if can_buy:
            for i in range(1, self.num_levels + 1):
                price = mid * (1 - spread_decimal * i)
                if self.price_precision == 0:
                    price = int(price)
                else:
                    price = round(price, self.price_precision)

                amount = round(self.order_size_usd / mid, self.amount_precision)

                try:
                    order = await self.client.place_order(
                        market_name=self.symbol,
                        amount_of_synthetic=Decimal(str(amount)),
                        price=Decimal(str(price)),
                        side=OrderSide.BUY,
                        post_only=True,
                        time_in_force=TimeInForce.GTT,
                        expire_time=datetime.now(timezone.utc) + timedelta(hours=1),
                    )
                    if order and order.data:
                        self.open_order_ids.append(order.data.id)
                        orders_placed += 1
                        self.total_orders_placed += 1
                except Exception as e:
                    logger.error(f"  Buy order failed (level {i}): {e}")

        # Place SELL orders (above mid)
        if can_sell:
            for i in range(1, self.num_levels + 1):
                price = mid * (1 + spread_decimal * i)
                if self.price_precision == 0:
                    price = int(price)
                else:
                    price = round(price, self.price_precision)

                amount = round(self.order_size_usd / mid, self.amount_precision)

                try:
                    order = await self.client.place_order(
                        market_name=self.symbol,
                        amount_of_synthetic=Decimal(str(amount)),
                        price=Decimal(str(price)),
                        side=OrderSide.SELL,
                        post_only=True,
                        time_in_force=TimeInForce.GTT,
                        expire_time=datetime.now(timezone.utc) + timedelta(hours=1),
                    )
                    if order and order.data:
                        self.open_order_ids.append(order.data.id)
                        orders_placed += 1
                        self.total_orders_placed += 1
                except Exception as e:
                    logger.error(f"  Sell order failed (level {i}): {e}")

        logger.info(f"  Grid: {orders_placed} orders @ ${mid:,.2f} (spread: {self.current_spread_bps:.1f}bps)")
        self.last_refresh_time = time.time()

    def _calculate_roc(self) -> float:
        """Calculate Rate of Change in bps over the ROC window."""
        if len(self.price_history) < 2:
            return 0.0

        window = min(len(self.price_history), self.roc_window)
        old_price = self.price_history[-window]
        new_price = self.price_history[-1]

        if old_price == 0:
            return 0.0

        return ((new_price - old_price) / old_price) * 10000  # bps

    def _calculate_dynamic_spread(self, roc: float) -> float:
        """
        Calculate dynamic spread based on ROC (v18 Qwen-calibrated).
        """
        abs_roc = abs(roc)

        if abs_roc < 5:
            spread = 4.0
        elif abs_roc < 10:
            spread = 6.0
        elif abs_roc < 20:
            spread = 8.0
        elif abs_roc < 30:
            spread = 12.0
        elif abs_roc < 50:
            spread = 15.0
        else:
            spread = 0.0  # Will trigger pause

        return spread

    def _should_pause(self, roc: float) -> bool:
        """Check if we should pause based on ROC."""
        return abs(roc) > self.roc_threshold_bps

    def _should_refresh(self, mid: float) -> bool:
        """Check if grid needs refresh (time or price move)."""
        # Time-based refresh
        if self.last_refresh_time:
            elapsed = time.time() - self.last_refresh_time
            if elapsed >= self.time_refresh_interval:
                logger.info(f"  Time-based refresh: {elapsed:.0f}s since last placement")
                return True

        # Price-move refresh
        if self.grid_center and mid:
            pct_move = abs(mid - self.grid_center) / self.grid_center * 100
            if pct_move >= self.grid_reset_pct:
                logger.info(f"  Price-move refresh: {pct_move:.2f}% from grid center")
                return True

        return False

    async def _check_fills(self):
        """Check for fills using get_trades API (v20 - real exchange data).

        Queries the x10 trades endpoint for actual fill records instead of
        guessing fills from vanished orders.
        """
        try:
            trades_resp = await self.client.account.get_trades(
                market_names=[self.symbol], limit=50
            )

            if not trades_resp or not trades_resp.data:
                return

            trades = trades_resp.data

            if self.last_trade_id is None:
                # First check - record the latest trade ID, don't count historical
                if trades:
                    self.last_trade_id = trades[0].id
                    logger.info(f"  Fill tracking initialized (latest id: {self.last_trade_id})")
                return

            # Find trades newer than last seen
            new_trades = []
            for t in trades:
                if t.id == self.last_trade_id:
                    break
                new_trades.append(t)

            if new_trades:
                self.last_trade_id = new_trades[0].id

                for t in new_trades:
                    price = float(t.price)
                    qty = float(t.qty)
                    value = float(t.value)
                    fee = float(t.fee)
                    side = t.side
                    is_taker = t.is_taker

                    self.total_fills += 1
                    self.total_volume += value

                    logger.info(f"  FILL: {side} {qty:.6f} @ ${price:,.2f} (${value:,.2f}) fee=${fee:.4f} {'TAKER' if is_taker else 'MAKER'}")

            # Update open_order_ids for grid refresh logic
            open_orders = await self.client.account.get_open_orders(
                market_names=[self.symbol]
            )
            if open_orders and open_orders.data:
                self.open_order_ids = [o.id for o in open_orders.data]
            else:
                self.open_order_ids = []

        except Exception as e:
            logger.error(f"Error checking fills: {e}")

    async def run(self):
        """Main loop."""
        await self.initialize()

        logger.info("\nStarting grid MM (POST_ONLY, dynamic spread)...")
        logger.info("-" * 70)

        cycle = 0
        while True:
            try:
                cycle += 1

                # Get current price
                mid = await self._get_mid_price()
                if not mid:
                    logger.warning("  No price data, retrying...")
                    await asyncio.sleep(5)
                    continue

                # Track price for ROC
                self.price_history.append(mid)

                # Calculate ROC
                roc = self._calculate_roc()

                # Check for fills
                await self._check_fills()

                # Update dynamic spread
                new_spread = self._calculate_dynamic_spread(roc)
                if new_spread != self.current_spread_bps and new_spread > 0:
                    direction = "WIDENED" if new_spread > self.current_spread_bps else "TIGHTENED"
                    logger.info(f"  📊 SPREAD {direction}: {self.current_spread_bps:.1f} → {new_spread:.1f} bps (ROC: {roc:+.1f})")
                    self.current_spread_bps = new_spread

                # Handle pause state
                if self._should_pause(roc):
                    if not self.is_paused:
                        self.is_paused = True
                        self.pause_start_time = time.time()
                        logger.info(f"  ⏸️  PAUSED: ROC {roc:+.1f} bps exceeds {self.roc_threshold_bps} bps")
                        await self._cancel_all_orders()
                    await asyncio.sleep(2)
                    continue
                else:
                    if self.is_paused:
                        elapsed = time.time() - self.pause_start_time if self.pause_start_time else 999
                        if elapsed >= self.min_pause_duration:
                            self.is_paused = False
                            logger.info(f"  ▶️  RESUMED after {elapsed:.0f}s pause")
                        else:
                            await asyncio.sleep(2)
                            continue

                # Check if refresh needed
                if self._should_refresh(mid):
                    await self._cancel_all_orders()
                    self.grid_center = mid
                    await self._place_grid_orders(mid)

                # Status log every 60 seconds
                elapsed_min = (datetime.now() - self.session_start).total_seconds() / 60
                if cycle % 30 == 0:  # Every ~60s at 2s intervals
                    pos = await self._get_position()
                    balance = await self._get_balance()
                    pos_str = f"{pos['side']} {abs(pos['size']):.6f}" if pos else "FLAT"
                    fills_hr = self.total_fills / (elapsed_min / 60) if elapsed_min > 0 else 0
                    logger.info(f"\n[{elapsed_min:.0f}m] ${mid:,.2f} | ROC: {roc:+.1f}bps | Spread: {self.current_spread_bps:.1f}bps | LIVE")
                    logger.info(f"  Position: {pos_str}")
                    logger.info(f"  Volume: ${self.total_volume:.0f} | Fills: {self.total_fills} ({fills_hr:.1f}/hr)")
                    logger.info(f"  Balance: ${balance:.2f}")

                # First cycle: place initial grid
                if cycle == 1:
                    self.grid_center = mid
                    await self._place_grid_orders(mid)

                await asyncio.sleep(2)

            except KeyboardInterrupt:
                logger.info("\nShutting down...")
                await self._cancel_all_orders()
                await self.client.close()
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(10)

        # Final stats
        elapsed_min = (datetime.now() - self.session_start).total_seconds() / 60
        logger.info("=" * 70)
        logger.info(f"SESSION SUMMARY ({elapsed_min:.0f} minutes)")
        logger.info(f"  Orders Placed: {self.total_orders_placed}")
        logger.info(f"  Fills: {self.total_fills}")
        logger.info(f"  Volume: ${self.total_volume:.2f}")
        logger.info("=" * 70)


async def main():
    mm = GridMarketMakerExtended(
        symbol="BTC-USD",
        order_size_usd=50.0,           # $50 per order (conservative start)
        num_levels=2,                   # 2 levels per side
        max_inventory_pct=100.0,        # Max 100% of balance as position
        roc_threshold_bps=50.0,         # Pause above 50 bps ROC
        min_pause_duration=120,         # 2 min pause minimum
        time_refresh_interval=300,      # Refresh every 5 minutes
    )
    await mm.run()


if __name__ == "__main__":
    asyncio.run(main())
