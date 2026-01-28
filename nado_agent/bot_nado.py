#!/usr/bin/env python3
"""
Nado Trading Bot - Zero-Fee High Volume Strategy
Scans orderbook, uses trend analysis, finds best setups, self-learning

Features:
- Full orderbook analysis
- Trend/technical analysis on all markets
- LLM-powered trade decisions
- Self-learning from past trades (every 30 min)
- Background monitoring between decision cycles

Nado DEX features:
- Up to 20x leverage on BTC, ETH, SOL, BNB, XRP
- USDT0 settlement
- Zero maker fees (rebates!)
- Built on Ink L2

Usage:
    python -m nado_agent.bot_nado --dry-run
    python -m nado_agent.bot_nado --live
    python -m nado_agent.bot_nado --live --interval 600
"""

import os
import sys
import time
import asyncio
import logging
import argparse
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from dotenv import load_dotenv

# Add parent directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load environment variables from project root
project_root_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
load_dotenv(dotenv_path=project_root_env, override=True)

from llm_agent.llm import LLMTradingAgent
from llm_agent.self_learning import SelfLearning
from trade_tracker import TradeTracker
from dexes.nado.nado_sdk import NadoSDK
from nado_agent.data.nado_fetcher import NadoDataFetcher
from nado_agent.execution.nado_executor import NadoTradeExecutor

# Ensure logs directory exists
os.makedirs('logs', exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s',
    handlers=[
        logging.FileHandler('logs/nado_bot.log'),
        logging.StreamHandler()
    ]
)

# Suppress noisy loggers
logging.getLogger('urllib3').setLevel(logging.ERROR)
logging.getLogger('asyncio').setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


class NadoTradingBot:
    """
    Nado Trading Bot with LLM strategy and self-learning

    Zero maker fee exchange - focus on:
    1. Tight spreads (ETH, BTC, SOL preferred)
    2. High volume opportunities
    3. Technical setups with orderbook confirmation
    """

    def __init__(
        self,
        llm_api_key: str,
        dry_run: bool = True,
        check_interval: int = 600,  # 10 minutes (Qwen recommendation)
        position_size: float = 10.0,
        max_positions: int = 5,  # Focus on fewer positions
        max_spread_pct: float = 0.1,
        model: str = "qwen-max",
        self_learning_interval: int = 1800,  # 30 minutes
        testnet: bool = False,
        # Exit parameters (Qwen recommendations)
        take_profit_pct: float = 2.0,  # +2.0% TP (was 0.4%)
        stop_loss_pct: float = 1.5  # -1.5% SL (was 0.3%)
    ):
        """
        Initialize Nado trading bot

        Args:
            llm_api_key: LLM API key
            dry_run: If True, simulate trades
            check_interval: Seconds between decision cycles
            position_size: USD per trade
            max_positions: Maximum open positions
            max_spread_pct: Max spread to accept
            model: LLM model to use
            self_learning_interval: Seconds between self-learning cycles
            testnet: Use testnet instead of mainnet
        """
        self.dry_run = dry_run
        self.check_interval = check_interval
        self.position_size = position_size
        self.self_learning_interval = self_learning_interval
        self.testnet = testnet
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct

        # Per-symbol leverage (Qwen recommendations)
        self.symbol_leverage = {
            'ETH': 10,
            'BTC': 8,
            'SOL': 12,
            'ARB': 15,
            'DOGE': 10
        }

        env_mode = "TESTNET" if testnet else "MAINNET"
        logger.info(f"Initializing Nado Trading Bot ({'DRY-RUN' if dry_run else 'LIVE'} mode) [{env_mode}]")

        # Initialize Nado SDK
        wallet_address = os.getenv('NADO_WALLET_ADDRESS')
        linked_signer_key = os.getenv('NADO_LINKED_SIGNER_PRIVATE_KEY')
        subaccount_name = os.getenv('NADO_SUBACCOUNT_NAME', 'default')

        if not wallet_address or not linked_signer_key:
            raise ValueError("NADO_WALLET_ADDRESS and NADO_LINKED_SIGNER_PRIVATE_KEY must be set in .env")

        self.sdk = NadoSDK(
            wallet_address=wallet_address,
            linked_signer_private_key=linked_signer_key,
            subaccount_name=subaccount_name,
            testnet=testnet
        )
        logger.info(f"Nado SDK initialized (wallet: {wallet_address[:10]}...)")

        # Initialize data fetcher
        self.fetcher = NadoDataFetcher(nado_sdk=self.sdk)

        # Initialize trade tracker
        self.trade_tracker = TradeTracker(dex="nado")

        # Initialize executor (0.5% spread for zero-fee volume farming)
        self.executor = NadoTradeExecutor(
            nado_sdk=self.sdk,
            trade_tracker=self.trade_tracker,
            data_fetcher=self.fetcher,
            dry_run=dry_run,
            default_position_size=position_size,
            max_positions=max_positions,
            max_spread_pct=0.5  # Allow 0.5% spread for high volume
        )

        # Initialize LLM agent
        cambrian_api_key = os.getenv('CAMBRIAN_API_KEY', '')
        self.llm_agent = LLMTradingAgent(
            deepseek_api_key=llm_api_key,
            cambrian_api_key=cambrian_api_key,
            model=model,
            max_retries=2,
            daily_spend_limit=10.0,
            max_positions=max_positions
        )
        logger.info(f"LLM Model: {model}")

        # Initialize self-learning
        self.self_learning = SelfLearning(self.trade_tracker, min_trades_for_insight=5)
        self.last_self_learning_time = datetime.now()
        logger.info("Self-learning module initialized")

        # Priority symbols (Nado markets with best liquidity)
        self.priority_symbols = ['ETH', 'BTC', 'SOL']

        logger.info("")
        logger.info("=" * 60)
        logger.info("NADO BOT - SELF-LEARNING (Qwen-optimized)")
        logger.info("=" * 60)
        logger.info(f"  Check Interval: {check_interval}s ({check_interval // 60} min)")
        logger.info(f"  Position Size: ${position_size}")
        logger.info(f"  Max Positions: {max_positions}")
        logger.info(f"  Take Profit: +{take_profit_pct}%")
        logger.info(f"  Stop Loss: -{stop_loss_pct}%")
        logger.info(f"  Model: {model}")
        logger.info(f"  Per-symbol leverage: BTC 8x, ETH/DOGE 10x, SOL 12x, ARB 15x")
        logger.info("=" * 60)
        logger.info("Nado Trading Bot initialized successfully")

    def format_market_table(self, market_data: Dict[str, Dict]) -> str:
        """Format market data as table for LLM with indicators"""
        lines = []
        lines.append("=" * 120)
        lines.append("NADO MARKET DATA (Zero Maker Fees - Ink L2) + Binance Indicators")
        lines.append("=" * 120)
        lines.append(
            f"{'Symbol':<10} {'Price':>12} {'24h%':>8} "
            f"{'RSI':>6} {'MACD':>8} {'VolRatio':>9} {'Funding%':>10} {'Vol24h':>12}"
        )
        lines.append("-" * 120)

        # Sort by priority (BTC, ETH, SOL first), then alphabetically
        sorted_symbols = sorted(
            market_data.keys(),
            key=lambda s: (0 if s in self.priority_symbols else 1, s)
        )

        for symbol in sorted_symbols:
            data = market_data[symbol]
            price = data.get('price', 0)

            # New indicators from Binance
            change_24h = data.get('price_change_24h')
            rsi = data.get('rsi')
            macd = data.get('macd_histogram')
            vol_ratio = data.get('volume_ratio')
            funding = data.get('annualized_funding')  # Annualized funding %
            volume = data.get('volume_24h', 0) or 0

            # Format each indicator
            change_str = f"{change_24h:+.1f}%" if change_24h is not None else "N/A"
            rsi_str = f"{rsi:.0f}" if rsi is not None else "N/A"
            macd_str = f"{macd:+.4f}" if macd is not None else "N/A"
            vol_str = f"{vol_ratio:.1f}x" if vol_ratio is not None else "N/A"
            funding_str = f"{funding:+.1f}%" if funding is not None else "N/A"
            vol_24h_str = f"${volume/1e6:.1f}M" if volume >= 1e6 else f"${volume/1e3:.0f}K" if volume > 0 else "N/A"

            lines.append(
                f"{symbol:<10} ${price:>11,.2f} {change_str:>8} "
                f"{rsi_str:>6} {macd_str:>8} {vol_str:>9} {funding_str:>10} {vol_24h_str:>12}"
            )

        lines.append("=" * 120)

        # Add indicator legend
        lines.append("RSI: <30 oversold, >70 overbought | MACD: +bullish, -bearish | Funding: +longs pay, -shorts pay")

        return "\n".join(lines)

    def format_positions(self, positions: List[Dict]) -> str:
        """Format open positions for display"""
        if not positions:
            return "Open Positions: None"

        lines = ["Open Positions:"]
        lines.append(f"{'Symbol':<8} {'Side':<6} {'Entry':>12} {'Size':>10} {'P&L':>12}")
        lines.append("-" * 60)

        for pos in positions:
            symbol = pos.get('symbol', 'N/A')
            side = pos.get('side', 'N/A')
            entry = pos.get('entry_price', 0)
            size = pos.get('size', 0)
            pnl = pos.get('unrealized_pnl', 0)

            pnl_str = f"${pnl:+.2f}"
            lines.append(f"{symbol:<8} {side:<6} ${entry:>11,.2f} {size:>10.6f} {pnl_str:>12}")

        return "\n".join(lines)

    async def run_self_learning(self):
        """Run self-learning analysis"""
        logger.info("")
        logger.info("=" * 60)
        logger.info("SELF-LEARNING CYCLE")
        logger.info("=" * 60)

        context = self.self_learning.generate_learning_context(hours=168)  # Last 7 days
        if context:
            for line in context.split('\n'):
                logger.info(line)
        else:
            logger.info("Not enough trades for self-learning insights yet")

        self.last_self_learning_time = datetime.now()
        logger.info("=" * 60)

    async def run_once(self):
        """Run single decision cycle"""
        current_time = datetime.now()

        # Check if it's time for self-learning
        time_since_learning = (current_time - self.last_self_learning_time).total_seconds()
        if time_since_learning >= self.self_learning_interval:
            await self.run_self_learning()

        # Cycle header
        logger.info("")
        logger.info("")
        logger.info("=" * 80)
        logger.info(f"NADO DECISION CYCLE | {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 80)
        logger.info("")

        try:
            # Initialize fetcher if needed
            await self.fetcher.initialize()

            # Fetch account summary
            account = await self.fetcher.fetch_account_summary()
            account_balance = account.get('account_value', 0)
            logger.info(f"Account Balance: ${account_balance:.2f}")

            # Fetch open positions
            positions = await self.fetcher.fetch_positions()
            logger.info("")
            logger.info(self.format_positions(positions))

            # Fetch all market data
            logger.info("")
            logger.info("Fetching market data...")
            market_data = await self.fetcher.fetch_all_markets()

            if not market_data:
                logger.warning("No market data available - skipping cycle")
                return

            logger.info(f"Loaded {len(market_data)} markets")
            logger.info("")

            # Format market table
            market_table = self.format_market_table(market_data)
            logger.info(market_table)

            # Get trade history
            recent_trades = self.trade_tracker.get_recent_trades(hours=24, limit=10)
            trade_history = ""
            if recent_trades:
                trade_history = "\n\nRECENT TRADES (Last 24h):\n"
                for trade in recent_trades[-5:]:
                    symbol = trade.get('symbol', 'N/A')
                    side = trade.get('side', 'N/A')
                    pnl = trade.get('pnl') or 0  # Handle None pnl
                    status = trade.get('status', 'N/A')
                    trade_history += f"  {symbol} {side}: ${pnl:+.2f} ({status})\n"

            # Get self-learning context
            learning_context = self.self_learning.generate_learning_context(hours=168)

            # Build prompt
            analyzed_symbols = list(market_data.keys())
            prompt = self.llm_agent.prompt_formatter.format_trading_prompt(
                market_table=market_table,
                open_positions=positions,
                account_balance=account_balance,
                trade_history=trade_history,
                analyzed_tokens=analyzed_symbols,
                dex_name="Nado",
                learning_context=learning_context
            )

            # Get LLM decision
            logger.info("")
            logger.info("Getting trading decision from LLM...")

            result = self.llm_agent.model_client.query(
                prompt=prompt,
                max_tokens=1500,  # More tokens for multiple decisions
                temperature=0.3   # Slightly more creative for finding trades
            )

            if not result:
                logger.error("LLM query failed")
                return

            # Log LLM response
            logger.info("")
            logger.info("=" * 60)
            logger.info("LLM RESPONSE:")
            logger.info("=" * 60)
            for line in result["content"].split('\n'):
                logger.info(line)
            logger.info("=" * 60)

            # Parse decisions
            parsed_decisions = self.llm_agent.response_parser.parse_multiple_decisions(result["content"])

            if not parsed_decisions:
                logger.info("No actionable decisions from LLM")
                return

            # Validate and execute decisions
            logger.info("")
            logger.info(f"Processing {len(parsed_decisions)} decisions...")

            for decision in parsed_decisions:
                symbol = decision.get('symbol')
                action = decision.get('action', '').upper()
                confidence = decision.get('confidence', 0.5)
                reason = decision.get('reason', '')

                # Skip if symbol not in our market data
                if symbol not in market_data and action in ['BUY', 'SELL']:
                    logger.warning(f"Skipping {action} {symbol} - not a Nado market")
                    continue

                # Check spread for new positions (0.5% for high volume strategy)
                if action in ['BUY', 'SELL']:
                    spread = market_data.get(symbol, {}).get('spread_pct', 999)
                    if spread > 0.5:  # 0.5% max spread for zero-fee volume farming
                        logger.warning(f"Skipping {action} {symbol} - spread too wide ({spread:.3f}%)")
                        continue

                logger.info(f"Executing: {action} {symbol} (confidence: {confidence:.2f})")

                exec_result = await self.executor.execute_decision({
                    'action': action,
                    'symbol': symbol,
                    'confidence': confidence,
                    'reason': reason
                })

                if exec_result.get('success'):
                    logger.info(f"  SUCCESS: {exec_result}")
                else:
                    logger.warning(f"  FAILED: {exec_result.get('error')}")

            # Cycle complete
            logger.info("")
            logger.info("=" * 60)
            logger.info(f"CYCLE COMPLETE | {datetime.now().strftime('%H:%M:%S')}")
            logger.info("=" * 60)

        except Exception as e:
            logger.error(f"Cycle error: {e}", exc_info=True)

    async def run_background_monitor(self):
        """
        Background monitor that runs between decision cycles
        Checks for exit conditions more frequently
        """
        monitor_interval = 30  # Check every 30 seconds for fast exits

        while True:
            await asyncio.sleep(monitor_interval)

            try:
                positions = await self.fetcher.fetch_positions()
                if not positions:
                    continue

                for pos in positions:
                    symbol = pos.get('symbol')
                    entry = pos.get('entry_price', 0)
                    size = pos.get('size', 0)

                    # Calculate P&L percentage
                    if entry > 0 and size > 0:
                        current_bbo = await self.fetcher.fetch_bbo(symbol)
                        if current_bbo:
                            current_price = current_bbo.get('mid_price', 0)
                            if pos['side'] == 'LONG':
                                pnl_pct = ((current_price - entry) / entry) * 100
                            else:
                                pnl_pct = ((entry - current_price) / entry) * 100

                            # Exit conditions based on Qwen recommendations
                            if pnl_pct >= self.take_profit_pct:  # +2.0% TP default
                                logger.info(f"TAKE PROFIT: {symbol} +{pnl_pct:.2f}% (target: {self.take_profit_pct}%)")
                                await self.executor._close_position(symbol, f"Take profit +{pnl_pct:.2f}%")

                            elif pnl_pct <= -self.stop_loss_pct:  # -1.5% SL default
                                logger.info(f"STOP LOSS: {symbol} {pnl_pct:.2f}% (threshold: -{self.stop_loss_pct}%)")
                                await self.executor._close_position(symbol, f"Stop loss {pnl_pct:.2f}%")

            except Exception as e:
                logger.debug(f"Background monitor error: {e}")

    async def run(self):
        """Main bot loop"""
        logger.info("Starting Nado Trading Bot")
        logger.info(f"Check interval: {self.check_interval}s ({self.check_interval // 60} min)")
        logger.info(f"Self-learning interval: {self.self_learning_interval}s ({self.self_learning_interval // 60} min)")
        logger.info(f"Position size: ${self.position_size}")

        # Verify linked signer before starting
        logger.info("Verifying linked signer authorization...")
        is_verified = await self.sdk.verify_linked_signer()
        if not is_verified:
            logger.error("Linked signer not authorized! Please enable 1-Click Trading in Nado UI first.")
            logger.error("Run: python scripts/generate_nado_linked_signer.py")
            logger.error("Then paste the ADDRESS into Nado UI > Settings > 1-Click Trading")
            return

        logger.info("Linked signer verified!")

        # Start background monitor
        monitor_task = asyncio.create_task(self.run_background_monitor())

        try:
            while True:
                await self.run_once()

                next_cycle = datetime.now() + timedelta(seconds=self.check_interval)
                logger.info("")
                logger.info(f"Next cycle at: {next_cycle.strftime('%H:%M:%S')} (in {self.check_interval}s)")
                logger.info("")

                await asyncio.sleep(self.check_interval)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            monitor_task.cancel()
        except Exception as e:
            logger.error(f"Bot crashed: {e}", exc_info=True)
            monitor_task.cancel()


async def test_connection(testnet: bool = True):
    """Test connection to Nado"""
    logger.info("=" * 60)
    logger.info(f"Testing Nado {'Testnet' if testnet else 'Mainnet'} Connection")
    logger.info("=" * 60)

    wallet_address = os.getenv('NADO_WALLET_ADDRESS')
    linked_signer_key = os.getenv('NADO_LINKED_SIGNER_PRIVATE_KEY')
    subaccount_name = os.getenv('NADO_SUBACCOUNT_NAME', 'default')

    if not wallet_address or not linked_signer_key:
        logger.error("Missing credentials. Set NADO_WALLET_ADDRESS and NADO_LINKED_SIGNER_PRIVATE_KEY in .env")
        return False

    logger.info(f"Wallet: {wallet_address}")
    logger.info(f"Subaccount: {subaccount_name}")

    try:
        sdk = NadoSDK(
            wallet_address=wallet_address,
            linked_signer_private_key=linked_signer_key,
            subaccount_name=subaccount_name,
            testnet=testnet
        )

        # Test 1: Verify linked signer
        logger.info("\n1. Verifying linked signer...")
        is_verified = await sdk.verify_linked_signer()
        if is_verified:
            logger.info("   Linked signer: VERIFIED")
        else:
            logger.warning("   Linked signer: NOT VERIFIED")
            logger.warning("   Please enable 1-Click Trading in Nado UI first")

        # Test 2: Get products
        logger.info("\n2. Fetching products...")
        products = await sdk.get_products()
        if products:
            logger.info(f"   Found {len(products)} products:")
            for p in products[:5]:
                logger.info(f"   - {p.get('symbol')}")
        else:
            logger.warning("   No products found")

        # Test 3: Get balance
        logger.info("\n3. Fetching balance...")
        balance = await sdk.get_balance()
        if balance is not None:
            logger.info(f"   Balance: ${balance:.2f}")
        else:
            logger.warning("   Could not fetch balance")

        # Test 4: Get positions
        logger.info("\n4. Fetching positions...")
        positions = await sdk.get_positions()
        if positions:
            logger.info(f"   Found {len(positions)} positions")
        else:
            logger.info("   No open positions")

        logger.info("\n" + "=" * 60)
        logger.info("Connection test complete!")
        logger.info("=" * 60)
        return True

    except Exception as e:
        logger.error(f"Connection test failed: {e}", exc_info=True)
        return False


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Nado Trading Bot")
    parser.add_argument("--live", action="store_true", help="Enable live trading")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Dry run mode")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--test", action="store_true", help="Test connection only")
    parser.add_argument("--testnet", action="store_true", help="Use testnet")
    parser.add_argument("--interval", type=int, default=600, help="Check interval in seconds (default: 600 = 10 min)")
    parser.add_argument("--position-size", type=float, default=10.0, help="USD per trade")
    parser.add_argument("--max-positions", type=int, default=5, help="Max open positions")
    parser.add_argument("--model", type=str, default="qwen-max", help="LLM model")

    args = parser.parse_args()

    # Test connection mode
    if args.test:
        asyncio.run(test_connection(testnet=args.testnet))
        return

    # Determine mode
    dry_run = not args.live
    if args.live:
        logger.warning("LIVE TRADING MODE ENABLED")
    else:
        logger.info("Dry-run mode (no real trades)")

    # Get API keys - Use OpenRouter for Qwen
    llm_api_key = os.getenv("OPEN_ROUTER")
    if not llm_api_key:
        logger.error("OPEN_ROUTER not set in .env")
        sys.exit(1)

    nado_wallet = os.getenv("NADO_WALLET_ADDRESS")
    nado_key = os.getenv("NADO_LINKED_SIGNER_PRIVATE_KEY")
    if not nado_wallet or not nado_key:
        logger.error("NADO_WALLET_ADDRESS and NADO_LINKED_SIGNER_PRIVATE_KEY not set")
        logger.error("Run: python scripts/generate_nado_linked_signer.py")
        sys.exit(1)

    # Initialize bot
    bot = NadoTradingBot(
        llm_api_key=llm_api_key,
        dry_run=dry_run,
        check_interval=args.interval,
        position_size=args.position_size,
        max_positions=args.max_positions,
        model=args.model,
        testnet=args.testnet
    )

    # Run
    if args.once:
        logger.info("Running single decision cycle...")
        asyncio.run(bot.run_once())
    else:
        asyncio.run(bot.run())


if __name__ == "__main__":
    main()
