#!/usr/bin/env python3
"""
Long-Only Swing Trader - Runs alongside grid bots to catch uptrends

Scans Hibachi, Nado, and Paradex for LONG setups only, excluding tokens that grid bots trade.
Uses the v9 scoring system but ONLY for long entries.
"""

import os
import sys
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from llm_agent.llm import LLMTradingAgent
from llm_agent.data.sentiment_fetcher import SentimentFetcher

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler('logs/long_swing_trader.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Tokens to exclude (grid bots are trading these)
GRID_BOT_TOKENS = {'ETH', 'BTC'}

# Dynamic Position Sizing based on VALUE CEILING (not account size)
# This is the TOTAL VALUE we're willing to have deployed across ALL swing positions
TOTAL_VALUE_CEILING = 100.0  # Max $100 total across all swings
MIN_TRADE_SIZE_USD = 10.0    # Don't bother with trades under $10
MAX_SINGLE_TRADE_USD = 40.0  # Never more than $40 per single trade
MAX_POSITIONS = 5            # More positions allowed with smaller sizes

# Long-only prompt override
LONG_ONLY_PROMPT = """
🚀 **LONG-ONLY MODE ACTIVE** 🚀

You are in BULL MARKET SWING TRADE mode. The market is pumping and we want to catch uptrends.

**CRITICAL RULES:**
1. ONLY output LONG trades - NO SHORTS under any circumstances
2. Skip BTC and ETH - those are being traded by grid bots
3. Focus on ALTs that are showing strength: SOL, LINK, AVAX, ARB, OP, etc.
4. Low liquidity tokens with clear setups = BONUS (exchange rewards this)

**IDEAL SETUP:**
- RSI 40-60 (not overbought yet, room to run)
- MACD turning bullish or already positive
- Volume increasing
- Price breaking resistance or bouncing off support
- Score >= 2.5 (can be lower for low-liq tokens)

**SIZE:** Dynamic $10-40 based on confidence, max 5 positions, $100 total ceiling

**REMEMBER:** We're swinging for profits to offset grid bot drawdown.
Quality over quantity. If nothing looks good, NO_TRADE is fine.
"""


class LongSwingTrader:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.llm_agent = LLMTradingAgent(
            deepseek_api_key=os.getenv('OPEN_ROUTER'),
            cambrian_api_key=os.getenv('CAMBRIAN_API_KEY'),
            model="qwen-max",
            max_retries=2,
            daily_spend_limit=5.0
        )
        self.sentiment = SentimentFetcher()
        self.exchanges = {}
        self.open_positions = {}  # Track our swing positions with value
        self.total_deployed_value = 0.0  # Track total $ deployed across all positions

    def get_remaining_budget(self) -> float:
        """Calculate remaining value budget (CEILING - currently deployed)"""
        return max(0, TOTAL_VALUE_CEILING - self.total_deployed_value)

    def calculate_position_size(self, confidence: float, price: float) -> tuple[float, float]:
        """
        Dynamic position sizing based on:
        1. Confidence level (0.6-1.0)
        2. Remaining value ceiling (not account size)

        Returns: (size_in_tokens, value_in_usd)
        """
        remaining = self.get_remaining_budget()

        if remaining < MIN_TRADE_SIZE_USD:
            logger.info(f"   Budget exhausted: ${remaining:.2f} remaining (ceiling: ${TOTAL_VALUE_CEILING})")
            return 0.0, 0.0

        # Scale by confidence: higher confidence = larger % of remaining budget
        # 0.6 confidence = 30% of remaining, 1.0 confidence = 60% of remaining
        confidence_multiplier = 0.3 + (confidence - 0.6) * 0.75  # 0.3 to 0.6 range

        # Calculate target value
        target_value = remaining * confidence_multiplier

        # Apply caps
        target_value = min(target_value, MAX_SINGLE_TRADE_USD)  # Never exceed max per trade
        target_value = max(target_value, MIN_TRADE_SIZE_USD)    # But at least min size

        # Final check: don't exceed remaining budget
        target_value = min(target_value, remaining)

        if target_value < MIN_TRADE_SIZE_USD:
            return 0.0, 0.0

        size_in_tokens = target_value / price

        logger.info(f"   💰 Dynamic sizing: ${target_value:.2f} ({confidence:.0%} conf, ${remaining:.2f} remaining of ${TOTAL_VALUE_CEILING})")

        return size_in_tokens, target_value

    async def init_exchanges(self):
        """Initialize exchange connections"""
        logger.info("Initializing exchanges...")

        # Hibachi
        try:
            from dexes.hibachi.hibachi_sdk import HibachiSDK

            self.exchanges['hibachi'] = {
                'sdk': HibachiSDK(
                    os.getenv('HIBACHI_PUBLIC_KEY'),
                    os.getenv('HIBACHI_PRIVATE_KEY'),
                    os.getenv('HIBACHI_ACCOUNT_ID')
                ),
                'name': 'Hibachi'
            }
            # Fetch markets to verify connection
            markets = await self.exchanges['hibachi']['sdk'].get_markets()
            symbols = [m['symbol'] for m in markets] if markets else []
            # Filter out grid bot tokens
            symbols = [s for s in symbols if s.split('/')[0] not in GRID_BOT_TOKENS]
            self.exchanges['hibachi']['symbols'] = symbols
            logger.info(f"  Hibachi: {len(symbols)} markets (excluding BTC/ETH)")
        except Exception as e:
            logger.warning(f"  Hibachi init failed: {e}")

        # Nado
        try:
            from dexes.nado.nado_sdk import NadoSDK
            from nado_agent.data.nado_fetcher import NadoDataFetcher

            nado_sdk = NadoSDK(
                wallet_address=os.getenv('NADO_WALLET_ADDRESS'),
                linked_signer_private_key=os.getenv('NADO_LINKED_SIGNER_PRIVATE_KEY'),
                subaccount_name=os.getenv('NADO_SUBACCOUNT_NAME', 'default')
            )

            self.exchanges['nado'] = {
                'sdk': nado_sdk,
                'fetcher': NadoDataFetcher(nado_sdk),
                'name': 'Nado'
            }
            logger.info("  Nado: Ready")
        except Exception as e:
            logger.warning(f"  Nado init failed: {e}")

        # Paradex
        try:
            from paradex_py import Paradex
            from paradex_agent.data.paradex_fetcher import ParadexDataFetcher

            # Initialize Paradex client (Python 3.9 compatible)
            paradex = Paradex(
                env='prod',
                l2_private_key=os.getenv('PARADEX_PRIVATE_SUBKEY'),
            )

            fetcher = ParadexDataFetcher(paradex_client=paradex)
            await fetcher.initialize()

            self.exchanges['paradex'] = {
                'client': paradex,
                'fetcher': fetcher,
                'name': 'Paradex'
            }
            symbols = fetcher.get_tradeable_symbols()
            # Filter out grid bot tokens
            symbols = [s for s in symbols if s not in GRID_BOT_TOKENS]
            self.exchanges['paradex']['symbols'] = symbols
            logger.info(f"  Paradex: {len(symbols)} markets (excluding BTC/ETH)")
        except Exception as e:
            logger.warning(f"  Paradex init failed: {e}")

    async def fetch_market_data(self, exchange: str) -> Dict:
        """Fetch market data from exchange"""
        try:
            ex = self.exchanges.get(exchange)
            if not ex:
                return {}

            if exchange == 'hibachi':
                sdk = ex['sdk']
                data = {}
                for symbol in ex.get('symbols', [])[:15]:  # Limit to 15 markets
                    try:
                        price = await sdk.get_price(symbol)
                        if price:
                            data[symbol] = {
                                'price': price,
                                'symbol': symbol
                            }
                    except:
                        continue
                return data

            elif exchange == 'nado':
                raw_data = await ex['fetcher'].fetch_all_markets()
                # Filter out grid bot tokens
                return {k: v for k, v in raw_data.items()
                        if k.replace('-PERP', '') not in GRID_BOT_TOKENS}

            elif exchange == 'paradex':
                fetcher = ex['fetcher']
                symbols = ex.get('symbols', [])[:15]
                data = {}
                for symbol in symbols:
                    try:
                        market_data = fetcher.fetch_market_data(symbol)
                        if market_data:
                            data[symbol] = market_data
                    except:
                        continue
                return data

        except Exception as e:
            logger.error(f"Market data error ({exchange}): {e}")
        return {}

    def format_market_table(self, data: Dict) -> str:
        """Format market data as table for LLM"""
        if not data:
            return "No market data available"

        lines = ["Symbol      | Price      | 24h%   | RSI   | MACD  | Volume"]
        lines.append("-" * 60)

        for symbol, info in sorted(data.items())[:20]:
            try:
                price = float(info.get('price') or info.get('last_price') or info.get('mid_price') or 0)
                change = float(info.get('price_change_24h') or info.get('change_24h') or info.get('change') or 0)
                rsi = float(info.get('rsi') or info.get('rsi_14') or 50)
                macd = float(info.get('macd') or info.get('macd_histogram') or 0)
                volume = float(info.get('volume_24h') or info.get('volume') or 0)

                lines.append(
                    f"{symbol:11} | ${price:>9.4f} | {change:>+5.1f}% | {rsi:>5.1f} | {macd:>+5.2f} | ${volume:,.0f}"
                )
            except (ValueError, TypeError) as e:
                continue

        return "\n".join(lines)

    async def analyze_and_trade(self, exchange: str):
        """Analyze exchange and make trade decisions"""
        logger.info(f"\n{'='*60}")
        logger.info(f"Analyzing {exchange.upper()} for LONG setups...")
        logger.info(f"{'='*60}")

        # Show budget status
        remaining = self.get_remaining_budget()
        logger.info(f"💰 Value Status: ${self.total_deployed_value:.2f} deployed / ${TOTAL_VALUE_CEILING} ceiling")
        logger.info(f"   Remaining budget: ${remaining:.2f} | Open positions: {len(self.open_positions)}/{MAX_POSITIONS}")

        if remaining < MIN_TRADE_SIZE_USD:
            logger.info("⚠️ Budget exhausted - skipping scan")
            return

        if len(self.open_positions) >= MAX_POSITIONS:
            logger.info("⚠️ Max positions reached - skipping scan")
            return

        # Fetch data
        market_data = await self.fetch_market_data(exchange)
        if not market_data:
            logger.info("No market data - skipping")
            return

        logger.info(f"Scanning {len(market_data)} markets (excluding BTC/ETH)")

        # Format for LLM
        market_table = self.format_market_table(market_data)

        # Get sentiment
        try:
            sentiment = await self.sentiment.fetch_all()
            sentiment_str = f"Fear/Greed: {sentiment.get('fear_greed', {}).get('value', 'N/A')}"
        except:
            sentiment_str = ""

        # Build prompt
        prompt = f"""
{LONG_ONLY_PROMPT}

═══════════════════════════════════════════════════════════════
EXCHANGE: {exchange.upper()}
═══════════════════════════════════════════════════════════════

{sentiment_str}

**MARKET DATA:**
{market_table}

**YOUR TASK:**
1. Scan ALL markets above for LONG setups
2. Score each potential trade using the v9 system
3. Output only HIGH-CONVICTION longs (score >= 2.5)
4. If nothing looks good, output NO_TRADE

**RESPONSE FORMAT:**
For each trade: SYMBOL|LONG|CONFIDENCE|REASONING
Example: SOL|LONG|0.75|RSI 42 bouncing, MACD crossing up, volume 1.8x

Or: NO_TRADE|No clear setups found
"""

        # Query LLM directly
        try:
            result = self.llm_agent.model_client.query(
                prompt=prompt,
                max_tokens=500,
                temperature=0.3
            )

            if result and result.get('content'):
                response_str = result['content']
            else:
                response_str = "NO_TRADE"

            logger.info(f"\nLLM Response:\n{response_str[:500]}")

            # Parse and execute trades
            await self.execute_trades(exchange, response_str, market_data)

        except Exception as e:
            logger.error(f"LLM error: {e}")

    async def execute_trades(self, exchange: str, response: str, market_data: Dict):
        """Parse LLM response and execute trades"""
        if "NO_TRADE" in response.upper():
            logger.info("No trades - LLM found no good setups")
            return

        ex = self.exchanges.get(exchange)
        if not ex:
            return

        # Parse trade signals
        lines = response.strip().split('\n')
        for line in lines:
            if '|LONG|' in line.upper():
                parts = line.split('|')
                if len(parts) >= 3:
                    symbol = parts[0].strip()
                    direction = parts[1].strip().upper()
                    try:
                        confidence = float(parts[2].strip())
                    except:
                        confidence = 0.6
                    reasoning = parts[3].strip() if len(parts) > 3 else ""

                    if direction == 'LONG' and confidence >= 0.6:
                        logger.info(f"\n🎯 TRADE SIGNAL: {symbol} LONG @ {confidence:.0%} confidence")
                        logger.info(f"   Reason: {reasoning}")

                        # Check if we have budget remaining
                        if self.get_remaining_budget() < MIN_TRADE_SIZE_USD:
                            logger.info(f"   ⚠️ Skipping - value ceiling reached (${self.total_deployed_value:.2f}/${TOTAL_VALUE_CEILING})")
                            continue

                        if len(self.open_positions) >= MAX_POSITIONS:
                            logger.info(f"   ⚠️ Skipping - max positions reached ({MAX_POSITIONS})")
                            continue

                        if self.dry_run:
                            logger.info(f"   [DRY RUN] Would open LONG on {symbol}")
                        else:
                            await self._execute_long(exchange, symbol, confidence, market_data)

    async def _execute_long(self, exchange: str, symbol: str, confidence: float, market_data: Dict):
        """Execute a long trade on the exchange with dynamic sizing"""
        try:
            ex = self.exchanges.get(exchange)
            if not ex:
                return

            # Get price
            info = market_data.get(symbol, {})
            price = float(info.get('price') or info.get('last_price') or info.get('mid_price') or 0)
            if price <= 0:
                logger.warning(f"   Cannot get price for {symbol}")
                return

            # Calculate dynamic size based on confidence and remaining value ceiling
            size, value_usd = self.calculate_position_size(confidence, price)
            if size <= 0:
                logger.info(f"   Skipping {symbol} - insufficient budget")
                return

            if exchange == 'hibachi':
                sdk = ex['sdk']
                # Find the full symbol format
                full_symbol = symbol
                for s in ex.get('symbols', []):
                    if symbol in s:
                        full_symbol = s
                        break

                # Place market buy order
                result = await sdk.create_market_order(
                    symbol=full_symbol,
                    side='BUY',
                    size=size
                )
                if result:
                    logger.info(f"   ✅ HIBACHI LONG opened: {symbol} size={size:.6f} @ ${price:.4f} (${value_usd:.2f})")
                    self.open_positions[f"hibachi_{symbol}"] = {
                        'symbol': symbol,
                        'side': 'LONG',
                        'size': size,
                        'entry_price': price,
                        'value_usd': value_usd,
                        'exchange': 'hibachi'
                    }
                    self.total_deployed_value += value_usd
                    logger.info(f"   📊 Total deployed: ${self.total_deployed_value:.2f} / ${TOTAL_VALUE_CEILING}")

            elif exchange == 'nado':
                sdk = ex['sdk']
                # Nado uses symbol format like "SOL-PERP"
                full_symbol = f"{symbol}-PERP" if not symbol.endswith('-PERP') else symbol

                # Get product info for lot size
                product = await sdk.get_product_by_symbol(full_symbol)
                if product:
                    # Get step size from product
                    import math
                    step_size_raw = product.get('size_increment') or product.get('base_increment')
                    if step_size_raw:
                        step_size = sdk._from_x18(int(step_size_raw))
                        # Round UP to step size
                        size = math.ceil(size / step_size) * step_size

                result = await sdk.create_market_order(
                    symbol=full_symbol,
                    is_buy=True,  # LONG = buy
                    amount=size
                )
                if result and result.get('status') == 'success':
                    # Recalculate actual value after lot size rounding
                    actual_value = size * price
                    logger.info(f"   ✅ NADO LONG opened: {symbol} size={size:.6f} @ ${price:.4f} (${actual_value:.2f})")
                    self.open_positions[f"nado_{symbol}"] = {
                        'symbol': symbol,
                        'side': 'LONG',
                        'size': size,
                        'entry_price': price,
                        'value_usd': actual_value,
                        'exchange': 'nado'
                    }
                    self.total_deployed_value += actual_value
                    logger.info(f"   📊 Total deployed: ${self.total_deployed_value:.2f} / ${TOTAL_VALUE_CEILING}")

            elif exchange == 'paradex':
                client = ex['client']
                from paradex_py.common.order import Order, OrderType, OrderSide

                full_symbol = f"{symbol}-USD-PERP" if not symbol.endswith('-USD-PERP') else symbol

                order = Order(
                    market=full_symbol,
                    order_type=OrderType.MARKET,
                    side=OrderSide.BUY,
                    size=str(size)
                )
                result = client.submit_order(order)
                if result:
                    logger.info(f"   ✅ PARADEX LONG opened: {symbol} size={size:.6f} @ ${price:.4f} (${value_usd:.2f})")
                    self.open_positions[f"paradex_{symbol}"] = {
                        'symbol': symbol,
                        'side': 'LONG',
                        'size': size,
                        'entry_price': price,
                        'value_usd': value_usd,
                        'exchange': 'paradex'
                    }
                    self.total_deployed_value += value_usd
                    logger.info(f"   📊 Total deployed: ${self.total_deployed_value:.2f} / ${TOTAL_VALUE_CEILING}")

        except Exception as e:
            logger.error(f"   ❌ Trade execution failed: {e}")

    async def run_once(self):
        """Run one scan cycle"""
        await self.init_exchanges()

        for exchange in self.exchanges:
            try:
                await self.analyze_and_trade(exchange)
            except Exception as e:
                logger.error(f"Error on {exchange}: {e}")

        logger.info("\n" + "="*60)
        logger.info("Scan complete")

    async def run_loop(self, interval: int = 600):
        """Run continuous scanning loop"""
        await self.init_exchanges()

        while True:
            for exchange in self.exchanges:
                try:
                    await self.analyze_and_trade(exchange)
                except Exception as e:
                    logger.error(f"Error on {exchange}: {e}")

            logger.info(f"\nSleeping {interval}s until next scan...")
            await asyncio.sleep(interval)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='Run once')
    parser.add_argument('--interval', type=int, default=600, help='Scan interval')
    parser.add_argument('--dry-run', action='store_true', help='Dry run mode (no live trades)')
    args = parser.parse_args()

    mode = "DRY RUN" if args.dry_run else "LIVE"
    logger.info(f"Starting Long Swing Trader in {mode} mode")

    trader = LongSwingTrader(dry_run=args.dry_run)

    if args.once:
        asyncio.run(trader.run_once())
    else:
        asyncio.run(trader.run_loop(args.interval))
