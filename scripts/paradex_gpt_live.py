#!/usr/bin/env python3.11
"""
Paradex GPT-Style Trading Bot

Tests the simplified, decisive GPT-like decision engine on Paradex.
Based on what GPT 5.1 Instant did right:
- Simple scoring (-2 to +2)
- Mechanical execution
- Tight TP/SL (3%/2%)
- No overthinking

Usage:
    python scripts/paradex_gpt_live.py --dry-run
    python scripts/paradex_gpt_live.py --live
"""

import os
import sys
import asyncio
import logging
import argparse
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dotenv import load_dotenv

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load environment
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(project_root, '.env'))

from orchestrator.simplified_decision_engine import SimplifiedDecisionEngine
from paradex_agent.data.paradex_fetcher import ParadexDataFetcher

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)
logging.getLogger('urllib3').setLevel(logging.ERROR)


class ParadexGPTBot:
    """
    Simplified GPT-style trading bot for Paradex

    Key principles:
    1. Score the market (-2 to +2)
    2. Execute mechanically based on score
    3. Tight risk management (3% TP, 2% SL)
    4. No analysis paralysis
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        dry_run: bool = True,
        interval: int = 300,
        position_size: float = 15.0
    ):
        self.dry_run = dry_run
        self.interval = interval
        self.position_size = position_size

        logger.info("=" * 60)
        logger.info("PARADEX GPT-STYLE BOT")
        logger.info(f"Mode: {'DRY-RUN' if dry_run else '🔴 LIVE'}")
        logger.info(f"Model: {model}")
        logger.info(f"Interval: {interval}s")
        logger.info(f"Position size: ${position_size}")
        logger.info("=" * 60)

        # Initialize Paradex client
        from paradex_py import ParadexSubkey

        self.paradex = ParadexSubkey(
            env='prod',
            l2_private_key=os.getenv('PARADEX_PRIVATE_SUBKEY'),
            l2_address=os.getenv('PARADEX_ACCOUNT_ADDRESS'),
        )
        logger.info("✅ Paradex client initialized")

        # Initialize data fetcher
        self.fetcher = ParadexDataFetcher(paradex_client=self.paradex)

        # Initialize simplified decision engine with GPT-4o
        api_key = os.getenv('OPEN_ROUTER')
        self.engine = SimplifiedDecisionEngine(api_key=api_key, model=model)
        logger.info(f"✅ Decision engine initialized: {model}")

        # Position tracking
        self.positions: Dict[str, Dict] = {}

        # Symbols to trade — majors with Binance kline data for technicals.
        # Expanded 2026-04-24 per user directive: analyze every liquid market
        # the exchange offers, not just BTC.
        self.symbols = [
            'BTC', 'ETH', 'SOL', 'AAVE', 'AVAX', 'UNI', 'LINK',
            'DOGE', 'XRP', 'SUI', 'LTC', 'BNB', 'ADA', 'NEAR',
        ]

    def get_binance_technicals(self, symbol: str) -> Dict:
        """Fetch technicals from Binance for a symbol"""
        try:
            # Get klines for indicators
            pair = f"{symbol}USDT"
            url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval=1h&limit=50"
            resp = requests.get(url, timeout=10)
            klines = resp.json()

            if not klines:
                return {}

            closes = [float(k[4]) for k in klines]
            volumes = [float(k[5]) for k in klines]
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]

            price = closes[-1]

            # RSI (14 period)
            deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains = [d if d > 0 else 0 for d in deltas[-14:]]
            losses = [-d if d < 0 else 0 for d in deltas[-14:]]
            avg_gain = sum(gains) / 14
            avg_loss = sum(losses) / 14
            if avg_loss == 0:
                rsi = 100
            else:
                rs = avg_gain / avg_loss
                rsi = 100 - (100 / (1 + rs))

            # MACD
            ema12 = sum(closes[-12:]) / 12
            ema26 = sum(closes[-26:]) / 26
            macd_line = ema12 - ema26
            signal_line = macd_line * 0.9  # Simplified
            macd_histogram = macd_line - signal_line

            # Volume ratio
            avg_volume = sum(volumes[-20:]) / 20
            current_volume = volumes[-1]
            volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1

            # Donchian Channel (20 period)
            dcu = max(highs[-20:])  # Upper
            dcl = min(lows[-20:])   # Lower
            dcm = (dcu + dcl) / 2   # Middle

            # Moving Averages
            ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else price
            ma40 = sum(closes[-40:]) / 40 if len(closes) >= 40 else price

            return {
                "indicators": {
                    "price": price,
                    "rsi": rsi,
                    "macd_histogram": macd_histogram,
                    "volume_ratio": volume_ratio,
                    "donchian_upper": dcu,
                    "donchian_middle": dcm,
                    "donchian_lower": dcl,
                    "ma20": ma20,
                    "ma40": ma40,
                }
            }
        except Exception as e:
            logger.error(f"Error fetching technicals for {symbol}: {e}")
            return {}

    def get_funding_rates(self) -> Dict[str, Dict[str, float]]:
        """Fetch funding rates from Binance"""
        try:
            url = "https://fapi.binance.com/fapi/v1/premiumIndex"
            resp = requests.get(url, timeout=10)
            data = resp.json()

            rates = {}
            for item in data:
                symbol = item['symbol'].replace('USDT', '')
                if symbol in self.symbols:
                    rates[symbol] = float(item['lastFundingRate'])

            return {"binance": rates}
        except Exception as e:
            logger.error(f"Error fetching funding rates: {e}")
            return {"binance": {}}

    async def get_paradex_balance(self) -> float:
        """Get Paradex account balance"""
        try:
            account = self.fetcher.fetch_account_summary()
            return float(account.get('account_value', 0))
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            return 0

    async def get_paradex_positions(self) -> List[Dict]:
        """Get open Paradex positions"""
        try:
            return self.fetcher.fetch_positions()
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return []

    async def execute_trade(self, decision: Dict) -> bool:
        """Execute a trade on Paradex"""
        symbol = decision.get('symbol')
        direction = decision.get('direction')
        size_usd = decision.get('size_usd', self.position_size)

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would {direction} {symbol} for ${size_usd:.2f}")
            return True

        try:
            # Get current price
            bbo = self.fetcher.fetch_bbo(symbol)
            if not bbo:
                logger.error(f"No BBO for {symbol}")
                return False

            price = bbo.get('mid_price', 0)
            if price == 0:
                return False

            # Calculate size and round UP to 5 decimal places (Paradex requirement)
            # Must round UP to ensure we meet minimum notional after rounding
            import math
            size = size_usd / price
            size = math.ceil(size * 100000) / 100000  # Round UP to 5 decimals

            # Place market order
            from paradex_py.common.order import Order, OrderType, OrderSide
            from decimal import Decimal

            order_side = OrderSide.Buy if direction == 'LONG' else OrderSide.Sell

            order = Order(
                market=f"{symbol}-USD-PERP",
                order_type=OrderType.Market,
                order_side=order_side,
                size=Decimal(str(size))
            )
            result = self.paradex.api_client.submit_order(order)

            if result:
                logger.info(f"✅ {direction} {symbol} @ ${price:,.2f} (${size_usd:.2f})")
                self.positions[symbol] = {
                    'direction': direction,
                    'entry_price': price,
                    'size': size,
                    'size_usd': size_usd,
                    'tp': decision.get('tp'),
                    'sl': decision.get('sl'),
                    'entry_time': datetime.now()
                }
                return True
            else:
                logger.error(f"Order failed for {symbol}")
                return False

        except Exception as e:
            logger.error(f"Trade execution error: {e}")
            return False

    async def check_exits(self):
        """Check if any positions should be closed"""
        if not self.positions:
            return

        for symbol, pos in list(self.positions.items()):
            try:
                bbo = self.fetcher.fetch_bbo(symbol)
                if not bbo:
                    continue

                current_price = bbo.get('mid_price', 0)
                entry_price = pos['entry_price']
                direction = pos['direction']
                tp = pos.get('tp')
                sl = pos.get('sl')

                # Calculate P&L %
                if direction == 'LONG':
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100
                    should_tp = tp and current_price >= tp
                    should_sl = sl and current_price <= sl
                else:
                    pnl_pct = ((entry_price - current_price) / entry_price) * 100
                    should_tp = tp and current_price <= tp
                    should_sl = sl and current_price >= sl

                # Check exit conditions
                if should_tp:
                    logger.info(f"🎯 TP HIT: {symbol} +{pnl_pct:.2f}%")
                    await self.close_position(symbol, "TP")
                elif should_sl:
                    logger.info(f"🛑 SL HIT: {symbol} {pnl_pct:.2f}%")
                    await self.close_position(symbol, "SL")

            except Exception as e:
                logger.error(f"Exit check error for {symbol}: {e}")

    async def close_position(self, symbol: str, reason: str):
        """Close a position"""
        if symbol not in self.positions:
            return

        pos = self.positions[symbol]

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would close {symbol} ({reason})")
            del self.positions[symbol]
            return

        try:
            # Determine close side
            from paradex_py.common.order import Order, OrderType, OrderSide
            from decimal import Decimal

            order_side = OrderSide.Sell if pos['direction'] == 'LONG' else OrderSide.Buy

            order = Order(
                market=f"{symbol}-USD-PERP",
                order_type=OrderType.Market,
                order_side=order_side,
                size=Decimal(str(pos['size'])),
                reduce_only=True
            )
            result = self.paradex.api_client.submit_order(order)

            if result:
                logger.info(f"✅ Closed {symbol} ({reason})")
                del self.positions[symbol]
            else:
                logger.error(f"Failed to close {symbol}")

        except Exception as e:
            logger.error(f"Close position error: {e}")

    async def run_cycle(self):
        """Run one decision cycle"""
        logger.info("")
        logger.info("=" * 50)
        logger.info(f"CYCLE | {datetime.now().strftime('%H:%M:%S')}")
        logger.info("=" * 50)

        # Check exits first
        await self.check_exits()

        # Get market data
        balance = await self.get_paradex_balance()
        positions = await self.get_paradex_positions()

        logger.info(f"Balance: ${balance:.2f}")
        logger.info(f"Open positions: {len(positions)}")

        # Get technicals for each symbol
        technical_data = {}
        for symbol in self.symbols:
            tech = self.get_binance_technicals(symbol)
            if tech:
                technical_data[symbol] = tech
                ind = tech.get('indicators', {})
                logger.info(
                    f"  {symbol}: ${ind.get('price', 0):,.0f} | "
                    f"RSI: {ind.get('rsi', 0):.0f} | "
                    f"MACD: {ind.get('macd_histogram', 0):+.2f}"
                )

        # Get funding rates
        funding_data = self.get_funding_rates()

        # Format balances for engine
        balances = {"paradex": balance}

        # Format positions for engine
        pos_dict = {"paradex": positions}

        # Get decision from simplified engine
        logger.info("")
        logger.info("Getting GPT decision...")

        # Check existing positions before trading
        existing_positions = {}
        for pos in positions:
            sym = pos.get('symbol', '').replace('-USD-PERP', '')
            side = pos.get('side', '')
            size = float(pos.get('size', 0))
            if size > 0:
                existing_positions[sym] = {
                    'side': side,
                    'size': size,
                    'pnl': float(pos.get('unrealized_pnl', 0))
                }
                logger.info(f"  📍 Existing: {sym} {side} (PnL: ${existing_positions[sym]['pnl']:+.2f})")

        try:
            decision = await self.engine.get_decision(
                balances=balances,
                positions=pos_dict,
                funding_data=funding_data,
                technical_data=technical_data
            )

            action = decision.get('decision', 'NO_TRADE')

            if action == 'TRADE':
                symbol = decision.get('symbol')
                direction = decision.get('direction')
                score = decision.get('score', 0)
                reasoning = decision.get('reasoning', '')

                logger.info(f"📊 DECISION: {direction} {symbol}")
                logger.info(f"   Score: {score} | Reason: {reasoning}")
                logger.info(f"   TP: ${decision.get('tp', 0):,.2f} | SL: ${decision.get('sl', 0):,.2f}")

                # Check if we already have a position in this symbol
                if symbol in existing_positions:
                    existing_side = existing_positions[symbol]['side']
                    existing_pnl = existing_positions[symbol]['pnl']

                    # Don't add to same direction
                    if (direction == 'LONG' and existing_side == 'LONG') or \
                       (direction == 'SHORT' and existing_side == 'SHORT'):
                        logger.warning(f"⚠️ SKIPPED: Already {existing_side} on {symbol} (PnL: ${existing_pnl:+.2f})")
                        return

                    # If opposite direction and losing, consider closing first
                    if existing_pnl < -5:  # Losing more than $5
                        logger.warning(f"⚠️ SKIPPED: Have opposite position on {symbol} with ${existing_pnl:.2f} loss")
                        return

                # Execute
                await self.execute_trade(decision)

            else:
                logger.info(f"📊 NO_TRADE: {decision.get('reasoning', 'No clear setup')}")

        except Exception as e:
            logger.error(f"Decision error: {e}")

        logger.info("=" * 50)

    async def run(self):
        """Main bot loop"""
        logger.info("")
        logger.info("🚀 Starting Paradex GPT Bot")
        logger.info(f"   Interval: {self.interval}s")
        logger.info(f"   Symbols: {', '.join(self.symbols)}")
        logger.info("")

        while True:
            try:
                await self.run_cycle()

                next_run = datetime.now() + timedelta(seconds=self.interval)
                logger.info(f"Next cycle: {next_run.strftime('%H:%M:%S')}")

                await asyncio.sleep(self.interval)

            except KeyboardInterrupt:
                logger.info("Bot stopped by user")
                break
            except Exception as e:
                logger.error(f"Cycle error: {e}")
                await asyncio.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="Paradex GPT-Style Bot")
    parser.add_argument("--live", action="store_true", help="Enable live trading")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Dry run mode")
    parser.add_argument("--model", type=str, default="gpt-4o", help="Model (gpt-4o, gpt-4o-mini)")
    parser.add_argument("--interval", type=int, default=300, help="Cycle interval in seconds")
    parser.add_argument("--size", type=float, default=15.0, help="Position size in USD")
    parser.add_argument("--once", action="store_true", help="Run once and exit")

    args = parser.parse_args()

    dry_run = not args.live

    bot = ParadexGPTBot(
        model=args.model,
        dry_run=dry_run,
        interval=args.interval,
        position_size=args.size
    )

    if args.once:
        asyncio.run(bot.run_cycle())
    else:
        asyncio.run(bot.run())


if __name__ == "__main__":
    main()
