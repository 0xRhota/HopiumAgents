#!/usr/bin/env python3
"""
Extended Trading Bot - Main Entry Point
Mirrors Hibachi bot structure, adapted for Extended DEX (Starknet)

REQUIRES: Python 3.11+ (SDK dependency)
Run with: python3.11 -m extended_agent.bot_extended --dry-run

Usage:
    python3.11 -m extended_agent.bot_extended --dry-run
    python3.11 -m extended_agent.bot_extended --live
    python3.11 -m extended_agent.bot_extended --dry-run --once
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

# Reuse shared LLM system
from llm_agent.llm import LLMTradingAgent
from trade_tracker import TradeTracker

# Extended-specific imports
from extended_agent.execution.extended_executor import (
    ExtendedTradeExecutor,
    create_extended_executor_from_env
)
from extended_agent.execution.strategy_b_exit_rules import StrategyBExitRules
from extended_agent.execution.strategy_c_copy_whale import StrategyC_CopyWhale as StrategyCCopyWhale
from extended_agent.execution.strategy_c_smart_copy import StrategyCSmartCopy
from extended_agent.execution.strategy_d_pairs_trade import StrategyDPairsTrade
from extended_agent.execution.strategy_e_self_improving_pairs import StrategyESelfImprovingPairs
from extended_agent.execution.fast_exit_monitor import FastExitMonitor
from extended_agent.data.extended_aggregator import ExtendedMarketDataAggregator
from llm_agent.data.sentiment_fetcher import SentimentFetcher
from llm_agent.shared_learning import SharedLearning

# Load environment variables from project root
project_root_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
load_dotenv(dotenv_path=project_root_env, override=True)

# Configure clean, human-readable logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s',
    handlers=[
        logging.FileHandler('logs/extended_bot.log'),
        logging.StreamHandler()
    ]
)

# Suppress noisy loggers
logging.getLogger('extended_agent.data.extended_fetcher').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.ERROR)
logging.getLogger('asyncio').setLevel(logging.ERROR)
logging.getLogger('aiohttp').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info(f"Loaded environment variables from: {project_root_env}")


class ExtendedTradingBot:
    """Extended trading bot - mirrors Hibachi bot structure for Starknet perps"""

    def __init__(
        self,
        cambrian_api_key: str,
        deepseek_api_key: str,
        dry_run: bool = True,
        check_interval: int = 300,  # 5 minutes
        position_size: float = 10.0,  # $10 per trade (~20% of $47 account)
        max_positions: int = 5,
        max_position_age_minutes: int = 240,  # 4 hours
        model: str = "qwen-max",  # LLM model to use (Qwen - Alpha Arena winner)
        strategy: str = "B"  # Strategy: "B" = LLM-based, "C" = Copy whale
    ):
        """
        Initialize Extended trading bot

        Args:
            cambrian_api_key: Cambrian API key for macro context
            deepseek_api_key: LLM API key (DeepSeek or OpenRouter depending on model)
            dry_run: If True, simulate trades without execution
            check_interval: Seconds between decision checks (default: 300 = 5 min)
            position_size: USD per trade (default: $10)
            max_positions: Max open positions (default: 5)
            max_position_age_minutes: Max position age in minutes before auto-close (default: 240)
            model: LLM model to use (default: deepseek-chat, options: qwen-max)
            strategy: "B" = LLM-based trading, "C" = Copy whale 0x335f (BTC Scalper)
        """
        self.dry_run = dry_run
        self.check_interval = check_interval
        self.strategy = strategy.upper()

        logger.info(f"Initializing Extended Trading Bot ({['LIVE', 'DRY-RUN'][dry_run]} mode)")

        # Initialize Extended SDK via executor factory
        self.trade_tracker = TradeTracker(dex="extended")

        # Create executor (handles SDK init internally)
        self.executor = create_extended_executor_from_env(
            trade_tracker=self.trade_tracker,
            dry_run=dry_run
        )

        if not self.executor:
            raise ValueError("Failed to create Extended executor - check credentials")

        # Set executor parameters
        self.executor.default_position_size = position_size
        self.executor.max_positions = max_positions
        self.executor.max_position_age_minutes = max_position_age_minutes

        # Initialize Extended data aggregator
        self.aggregator = ExtendedMarketDataAggregator(
            cambrian_api_key=cambrian_api_key,
            sdk=None,  # Will use REST API via custom SDK
            interval="5m",  # 5-minute candles for scalping
            candle_limit=100,
            macro_refresh_hours=12
        )
        logger.info("Using Extended DEX data (Starknet perps)")

        # Initialize LLM agent (same as Hibachi/Lighter)
        self.llm_agent = LLMTradingAgent(
            deepseek_api_key=deepseek_api_key,
            cambrian_api_key=cambrian_api_key,
            model=model,
            max_retries=2,
            daily_spend_limit=10.0,
            max_positions=max_positions
        )
        logger.info(f"LLM Model: {model}")

        # Initialize Sentiment Fetcher (Fear & Greed, funding rates)
        self.sentiment_fetcher = SentimentFetcher()
        logger.info("📊 Sentiment Fetcher initialized (Fear & Greed + funding)")

        # Initialize Shared Learning (cross-bot insights with Hibachi)
        self.shared_learning = SharedLearning(bot_name="extended")
        logger.info("🧠 Shared Learning initialized (cross-bot insights)")

        # Track last deep research cycle
        self.last_deep_research_time = datetime.now()
        self.deep_research_interval = 3600  # 1 hour

        # Track decisions for hourly review
        self.decision_history = []

        # Store position size for logging
        self.position_size = position_size

        # ═══════════════════════════════════════════════════════════════
        # STRATEGY SELECTION
        # ═══════════════════════════════════════════════════════════════
        self.copy_strategy = None  # Will be set if Strategy C
        self.pairs_strategy = None  # Will be set if Strategy D

        if self.strategy == "C":
            # Strategy C: Smart Copy Whale - BTC Scalper (switched 2025-12-04)
            self.copy_strategy = StrategyCSmartCopy()
            self.exit_rules = StrategyBExitRules()  # Still use exit rules for copied positions

            logger.info("")
            logger.info("=" * 70)
            logger.info("█" * 70)
            logger.info("█  STRATEGY C: SMART WHALE COPY")
            logger.info("█" * 70)
            logger.info("=" * 70)
            logger.info("")
            logger.info("  WHALE: 0x335f45392f8d87745aaae68f5c192849afd9b60e")
            logger.info("  NAME: BTC Scalper (0x335f)")
            logger.info("  STATS: $2M account, ~35 trades/hr, 48.4% WR, BTC ONLY")
            logger.info("")
            logger.info("  SMART COPY MECHANICS:")
            logger.info("    - Track whale POSITION CHANGES (not just snapshots)")
            logger.info("    - Copy NEW entries when whale opens")
            logger.info("    - Copy CLOSES when whale exits")
            logger.info("    - Copy FLIPS when whale reverses direction")
            logger.info("    - NO position stacking (skip if already have position)")
            logger.info("    - 2h cooldown after closing before re-entry")
            logger.info("")
            logger.info("  EXIT RULES: Strategy B TP/SL still apply")
            logger.info(f"    TP: +{self.exit_rules.TAKE_PROFIT_PCT}%  |  SL: -{self.exit_rules.STOP_LOSS_PCT}%")
            logger.info("")
            logger.info("  PHILOSOPHY: Copy whale's trades intelligently, not blindly")
            logger.info("=" * 70)
            logger.info("")
        elif self.strategy == "D":
            # Strategy D: Pairs Trade - volume generation while staying flat
            self.pairs_strategy = StrategyDPairsTrade(
                position_size_usd=position_size,
                hold_time_seconds=3600,  # 1 hour
                available_assets=["BTC-USD", "ETH-USD", "SOL-USD"],  # LLM picks any pair
                llm_agent=self.llm_agent  # Pass LLM for dynamic pair selection
            )
            self.exit_rules = None  # No exit rules - pairs strategy handles its own exits
            self.copy_strategy = None

            logger.info("")
            logger.info("=" * 70)
            logger.info("█" * 70)
            logger.info("█  STRATEGY D: PAIRS TRADE (VOLUME GENERATION)")
            logger.info("█" * 70)
            logger.info("=" * 70)
            logger.info("")
            logger.info(f"  ASSETS: {', '.join(self.pairs_strategy.available_assets)}")
            logger.info(f"  SIZE:  ${position_size} per leg (${position_size*2} total)")
            logger.info(f"  HOLD:  {self.pairs_strategy.hold_time_seconds/60:.0f} minutes")
            logger.info("")
            logger.info("  MECHANICS:")
            logger.info("    - LLM picks best pair from available assets")
            logger.info("    - Hold for 1 hour")
            logger.info("    - Close both positions")
            logger.info("    - Log actual PnL from exchange")
            logger.info("    - Repeat")
            logger.info("")
            logger.info("  GOAL: Generate volume while staying near break-even")
            logger.info("  NO LLM COSTS - pure mechanical execution")
            logger.info("=" * 70)
            logger.info("")
        elif self.strategy == "E":
            # Strategy E: Self-Improving Pairs Trade - learns from mistakes
            self.pairs_strategy = StrategyESelfImprovingPairs(
                position_size_usd=position_size,
                hold_time_seconds=3600,  # 1 hour
                available_assets=["BTC-USD", "ETH-USD", "SOL-USD"],  # LLM picks any pair
                llm_agent=self.llm_agent
            )
            self.exit_rules = None
            self.copy_strategy = None
            # Note: Strategy E logs its own banner in __init__
        else:
            # Strategy B: LLM-based trading (default)
            self.exit_rules = StrategyBExitRules()
            self.pairs_strategy = None

            logger.info("=" * 60)
            logger.info("EXTENDED BOT - STRATEGY B: FIXED TP/SL + FAST EXIT")
            logger.info(f"  TP: +{self.exit_rules.TAKE_PROFIT_PCT}%  |  SL: -{self.exit_rules.STOP_LOSS_PCT}%  |  Max Hold: {self.exit_rules.MAX_HOLD_HOURS}h (if profitable)")
            logger.info("  Fast Exit: 30s monitoring (FREE - no LLM)")
            logger.info("  Philosophy: Fixed targets based on backtest (2025-12-02)")
            logger.info("=" * 60)

        # Initialize Fast Exit Monitor (checks every 30s, NO LLM cost)
        self.fast_exit_monitor = FastExitMonitor(
            executor=self.executor,
            exit_rules=self.exit_rules,
            trade_tracker=self.trade_tracker,
            enabled=True  # Always enabled for faster exits
        )
        self.fast_exit_task = None  # Will be set when run() starts

        logger.info("Extended Trading Bot initialized successfully")

        # Log prompt version
        prompt_version = self.llm_agent.prompt_formatter.get_prompt_version()
        logger.info(f"Active Prompt Version: {prompt_version}")

    async def run_once(self):
        """Run single decision cycle - mirrors Hibachi bot structure"""
        current_time = datetime.now()

        logger.info("=" * 80)
        logger.info(f"Starting decision cycle at {current_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 80)

        try:
            # Fetch all market data
            logger.info("Fetching market data from Extended...")
            market_data_dict = await self.aggregator.fetch_all_markets()

            if not market_data_dict:
                logger.warning("No market data available - skipping cycle")
                return

            logger.info(f"Fetched data for {len(market_data_dict)} markets")

            # Get current positions from executor
            logger.info("Fetching current positions...")
            raw_positions = await self.executor._fetch_open_positions()

            # Build open_positions list with enriched data
            open_positions = []
            for pos in raw_positions:
                symbol = pos.get('symbol')
                size = float(pos.get('size', 0))

                if size == 0:
                    continue

                side = pos.get('side', 'LONG')
                entry_price = pos.get('entry_price', 0)
                current_price = pos.get('mark_price', entry_price)
                pnl = pos.get('unrealized_pnl', 0)

                open_positions.append({
                    'symbol': symbol,
                    'side': side,
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'size': size,
                    'pnl': pnl
                })

            logger.info(f"   Found {len(open_positions)} open positions")

            # Check for stale positions first
            logger.info("Checking for stale positions...")
            stale_closed = await self.executor.check_stale_positions()
            if stale_closed:
                for symbol in stale_closed:
                    logger.info(f"   Aged out {symbol}")
                # Refresh positions after closing stale ones
                raw_positions = await self.executor._fetch_open_positions()
                open_positions = []
                for pos in raw_positions:
                    symbol = pos.get('symbol')
                    size = float(pos.get('size', 0))
                    if size == 0:
                        continue
                    side = pos.get('side', 'LONG')
                    entry_price = pos.get('entry_price', 0)
                    current_price = pos.get('mark_price', entry_price)
                    pnl = pos.get('unrealized_pnl', 0)
                    open_positions.append({
                        'symbol': symbol,
                        'side': side,
                        'entry_price': entry_price,
                        'current_price': current_price,
                        'size': size,
                        'pnl': pnl
                    })

            # ═══════════════════════════════════════════════════════════════
            # STRATEGY B/C EXIT RULES (skip for Strategy D)
            # Check hard exit rules BEFORE LLM decision
            # ═══════════════════════════════════════════════════════════════
            if self.exit_rules:
                logger.info("")
                logger.info("=" * 60)
                logger.info("CHECKING EXIT RULES (RUNNERS_RUN)")
                logger.info("=" * 60)
                self.exit_rules.log_status()

                forced_closes = []
                for position in open_positions[:]:  # Use slice copy
                    symbol = position.get('symbol', 'UNKNOWN')
                    side = position.get('side', 'UNKNOWN')
                    entry_price = position.get('entry_price', 0)
                    current_price = position.get('current_price', entry_price)

                    # Calculate P&L percentage
                    if entry_price and entry_price > 0:
                        if side == 'LONG':
                            pnl_pct = ((current_price - entry_price) / entry_price) * 100
                        else:  # SHORT
                            pnl_pct = ((entry_price - current_price) / entry_price) * 100
                    else:
                        pnl_pct = 0

                    # Get tracker data for entry timestamp
                    tracker_data = self.trade_tracker.get_open_trade_for_symbol(symbol) if self.trade_tracker else None

                    # Get market data for this symbol (for RSI/MACD)
                    market_data = market_data_dict.get(symbol, {})

                    # Build position dict for exit rules
                    position_for_rules = {
                        'symbol': symbol,
                        'side': side,
                        'entry_price': entry_price,
                        'current_price': current_price,
                        'pnl_pct': pnl_pct / 100  # Convert to decimal
                    }

                    # Check if should force close
                    should_close, reason = self.exit_rules.check_should_force_close(
                        position_for_rules,
                        market_data,
                        tracker_data
                    )

                    if should_close:
                        forced_closes.append((symbol, reason, pnl_pct))

                        # Execute forced close immediately
                        close_decision = {
                            'action': 'CLOSE',
                            'symbol': symbol,
                            'reasoning': f"EXIT RULE: {reason}",
                            'confidence': 1.0
                        }
                        close_result = await self.executor.execute_decision(close_decision)

                        if close_result.get('success'):
                            logger.info(f"   ✅ Forced close executed successfully")
                            self.exit_rules.unregister_position(symbol)
                            open_positions = [p for p in open_positions if p.get('symbol') != symbol]
                        else:
                            logger.error(f"   ❌ Forced close failed: {close_result.get('error')}")
                    else:
                        # Log position status for tracking
                        self.exit_rules.log_position_status(symbol, pnl_pct)

                if forced_closes:
                    logger.info(f"   📊 Executed {len(forced_closes)} forced closes")
                logger.info("=" * 60)
                logger.info("")

            # Format market table
            market_table = self.aggregator.format_market_table(market_data_dict)

            # Get account balance
            account_balance = await self.executor._fetch_account_balance()
            if not account_balance:
                account_balance = 0.0
            logger.info(f"Account balance: ${account_balance:.2f}")

            # Get trade history
            trade_history = ""
            if self.trade_tracker:
                recent_trades = self.trade_tracker.get_recent_trades(hours=24, limit=10)
                if recent_trades:
                    trade_history = "\n\nRECENT TRADING HISTORY (Last 24h):\n"
                    trade_history += "Symbol | Side | Entry Price | Exit Price | P&L | Status\n"
                    trade_history += "-" * 70 + "\n"
                    for trade in recent_trades[-10:]:
                        symbol = trade.get('symbol') or 'N/A'
                        side = (trade.get('side') or 'N/A').upper()
                        entry = trade.get('entry_price') or 0
                        exit_price = trade.get('exit_price')
                        pnl = trade.get('pnl') or 0
                        status = trade.get('status') or 'N/A'
                        exit_str = f"${exit_price:.4f}" if exit_price and exit_price != 'N/A' else 'N/A'
                        trade_history += f"{symbol} | {side} | ${entry:.4f} | {exit_str} | ${pnl:.2f} | {status}\n"

            # Get recently closed symbols
            recently_closed = self.trade_tracker.get_recently_closed_symbols(hours=2)

            # Extended markets list
            extended_symbols = list(market_data_dict.keys())

            # ═══════════════════════════════════════════════════════════════
            # SENTIMENT DATA - Fear & Greed, funding rates (2026-01-06)
            # ═══════════════════════════════════════════════════════════════
            sentiment_context = ""
            try:
                sentiment_data = await self.sentiment_fetcher.fetch_all()
                if sentiment_data:
                    sentiment_context = self.sentiment_fetcher.get_prompt_context(sentiment_data)
                    # Update shared learning with latest sentiment
                    self.shared_learning.update_sentiment(sentiment_data)
                    logger.info(f"[SENTIMENT] Fear & Greed: {sentiment_data.get('fear_greed', {}).get('value', 'N/A')} | Combined: {sentiment_data.get('combined_score', 'N/A')}")
            except Exception as e:
                logger.warning(f"[SENTIMENT] Could not fetch sentiment: {e}")

            # ═══════════════════════════════════════════════════════════════
            # SHARED LEARNING - Cross-bot insights (Extended <-> Hibachi)
            # ═══════════════════════════════════════════════════════════════
            shared_learning_context = ""
            try:
                shared_learning_context = self.shared_learning.get_prompt_context()
                if shared_learning_context:
                    logger.info("[SHARED] Cross-bot learning context added to LLM prompt")
            except Exception as e:
                logger.warning(f"[SHARED] Could not fetch shared learning: {e}")

            # Build kwargs based on prompt version
            prompt_kwargs = {
                "market_table": market_table,
                "open_positions": open_positions,
                "account_balance": account_balance,
                "hourly_review": None,
                "trade_history": trade_history,
                "recently_closed_symbols": recently_closed or [],
                "dex_name": "Extended",
                "analyzed_tokens": extended_symbols,
                "sentiment_context": sentiment_context,  # Fear & Greed, funding rates
                "shared_learning_context": shared_learning_context  # Cross-bot insights
            }

            # Extended: Skip macro context for high-frequency scalping
            logger.info("Extended mode: Skipping macro context (not useful for HF scalping)")

            # ═══════════════════════════════════════════════════════════════
            # STRATEGY BRANCHING: C = Copy Whale, B = LLM-based
            # ═══════════════════════════════════════════════════════════════
            if self.strategy == "C" and self.copy_strategy:
                logger.info("")
                logger.info("=" * 70)
                logger.info("STRATEGY C: COPY WHALE DECISION CYCLE")
                logger.info("=" * 70)

                # Get copy decisions based on whale's current positions
                decisions = await self.copy_strategy.get_copy_decisions(
                    our_positions=open_positions,
                    account_balance=account_balance
                )

                if not decisions:
                    logger.info("   No copy actions needed - our positions match whale's allocation")
                    return

                logger.info(f"   Found {len(decisions)} copy actions to execute")

                # Execute copy decisions
                for i, decision in enumerate(decisions, 1):
                    action = decision.get('action')
                    symbol = decision.get('symbol')
                    reasoning = decision.get('reasoning', 'Copy whale')

                    logger.info(f"\n[{i}/{len(decisions)}] {action} {symbol}")
                    logger.info(f"   Reasoning: {reasoning}")

                    result = await self.executor.execute_decision(decision)

                    if result.get('success'):
                        logger.info(f"   ✅ Copy executed successfully")

                        # Track the trade
                        if action in ["LONG", "SHORT"]:
                            self.trade_tracker.log_entry(
                                order_id=result.get('order_id'),
                                symbol=symbol,
                                side='buy' if action == 'LONG' else 'sell',
                                entry_price=result.get('price', 0),
                                size=result.get('size', 0),
                                notes="strategy_c_copy_whale"
                            )
                            self.exit_rules.register_position(symbol, 'LONG' if action == 'LONG' else 'SHORT')
                    else:
                        logger.error(f"   ❌ Copy failed: {result.get('error')}")

                logger.info("")
                logger.info("=" * 70)
                logger.info("STRATEGY C CYCLE COMPLETE")
                logger.info("=" * 70)
                return

            # ═══════════════════════════════════════════════════════════════
            # STRATEGY D/E: PAIRS TRADE (volume generation)
            # Strategy D = basic pairs, Strategy E = self-improving pairs
            # ═══════════════════════════════════════════════════════════════
            if self.strategy in ["D", "E"] and self.pairs_strategy:
                logger.info("")
                logger.info("=" * 70)
                logger.info(f"STRATEGY {self.strategy}: PAIRS TRADE CYCLE")
                logger.info("=" * 70)

                # Check for orphaned positions (one leg missing from pair)
                orphan_symbol = self.pairs_strategy.sync_with_positions(open_positions)
                if orphan_symbol:
                    logger.warning(f"🧹 Closing orphaned position: {orphan_symbol}")
                    close_decision = {
                        "action": "CLOSE",
                        "symbol": orphan_symbol,
                        "reasoning": "Orphaned position - other leg missing from pair"
                    }
                    result = await self.executor.execute_decision(close_decision)
                    if result.get('success'):
                        logger.info(f"   ✅ Closed orphan {orphan_symbol}")
                    else:
                        logger.error(f"   ❌ Failed to close orphan: {result.get('error')}")

                # Check if we should close existing pair
                elif await self.pairs_strategy.should_close_pair():
                    logger.info("Hold time elapsed - closing pair")
                    close_decisions = await self.pairs_strategy.get_close_decisions()

                    for decision in close_decisions:
                        result = await self.executor.execute_decision(decision)
                        if result.get('success'):
                            # Get actual PnL from result
                            pnl = result.get('pnl', 0)
                            price = result.get('price', 0)
                            self.pairs_strategy.record_exit(
                                decision['symbol'],
                                price,
                                pnl
                            )
                            logger.info(f"   ✅ Closed {decision['symbol']}")
                        else:
                            logger.error(f"   ❌ Failed to close {decision['symbol']}: {result.get('error')}")

                # Check if we should open new pair
                elif await self.pairs_strategy.should_open_pair(open_positions):
                    logger.info("Opening new pairs trade")
                    open_decisions = await self.pairs_strategy.get_open_decisions(account_balance, market_data_dict)

                    for decision in open_decisions:
                        result = await self.executor.execute_decision(decision)
                        if result.get('success'):
                            price = result.get('price', 0)
                            size = result.get('size', 0)
                            self.pairs_strategy.record_entry(
                                decision['symbol'],
                                price,
                                size
                            )
                            logger.info(f"   ✅ Opened {decision['action']} {decision['symbol']} @ ${price:.2f}")
                        else:
                            logger.error(f"   ❌ Failed to open {decision['symbol']}: {result.get('error')}")

                else:
                    # Waiting for hold period
                    remaining = self.pairs_strategy.get_time_remaining()
                    if remaining:
                        logger.info(f"   Pair active - {remaining/60:.1f} min remaining until close")
                    else:
                        logger.info("   No active pair and conditions not met for new pair")

                status = self.pairs_strategy.get_status()
                logger.info(f"   Stats: {status['stats']['total_trades']} trades")
                if 'accuracy' in status['stats']:
                    logger.info(f"   Accuracy: {status['stats']['accuracy']:.0%}")
                if 'bias' in status:
                    logger.info(f"   Bias: {status['bias']['current']:.2f} ({status['bias']['category']})")
                logger.info("")
                logger.info("=" * 70)
                logger.info(f"STRATEGY {self.strategy} CYCLE COMPLETE")
                logger.info("=" * 70)
                return

            # ═══════════════════════════════════════════════════════════════
            # STRATEGY B: LLM-BASED TRADING (default)
            # ═══════════════════════════════════════════════════════════════

            prompt = self.llm_agent.prompt_formatter.format_trading_prompt(**prompt_kwargs)

            # Get trading decision from LLM
            logger.info("Getting trading decision from LLM...")

            # Convert market_data_dict to list for validation
            all_symbols = list(market_data_dict.keys())

            # Call LLM with retries
            responses = []
            for attempt in range(self.llm_agent.max_retries + 1):
                logger.info(f"   LLM query attempt {attempt + 1}/{self.llm_agent.max_retries + 1}...")

                # Query model
                result = self.llm_agent.model_client.query(
                    prompt=prompt,
                    max_tokens=500,
                    temperature=0.1
                )

                if result is None:
                    logger.error(f"   LLM query failed (attempt {attempt + 1})")
                    continue

                # Add response to list
                responses.append(result["content"])

                # Log full LLM response
                logger.info("")
                logger.info("=" * 80)
                logger.info("LLM RESPONSE:")
                logger.info("=" * 80)
                for line in result["content"].split('\n'):
                    logger.info(line)
                logger.info("=" * 80)
                logger.info("")

                # Try parsing multiple decisions
                parsed_decisions = self.llm_agent.response_parser.parse_multiple_decisions(result["content"])
                if parsed_decisions is None or len(parsed_decisions) == 0:
                    logger.warning(f"   Parse failed (attempt {attempt + 1}), will retry with clearer prompt")

                    # Modify prompt for retry
                    if attempt < self.llm_agent.max_retries:
                        prompt += (
                            f"\n\nIMPORTANT: Analyze ALL {len(all_symbols)} markets and respond with decisions ONLY for markets with clear signals:\n"
                            "TOKEN: BTC\n"
                            "DECISION: BUY BTC\n"
                            "CONFIDENCE: 0.75\n"
                            "REASON: Your reasoning here\n\n"
                            "Do NOT add any other text before or after."
                        )
                    continue

                # Validate all decisions
                logger.info("")
                logger.info("=" * 80)
                logger.info(f"VALIDATING {len(parsed_decisions)} DECISIONS FROM LLM")
                logger.info("=" * 80)

                valid_decisions = []
                current_positions = open_positions.copy() if open_positions else []

                for idx, parsed in enumerate(parsed_decisions, 1):
                    symbol = parsed.get("symbol")
                    action = parsed.get("action", "").upper()
                    confidence = parsed.get("confidence", 0.5)
                    reason = parsed.get("reason", "No reason provided")

                    # Normalize symbol format for Extended (add -USD if not present)
                    if symbol and not symbol.endswith("-USD"):
                        symbol = f"{symbol}-USD"
                        parsed["symbol"] = symbol  # Update parsed dict

                    logger.info(f"\n[Decision {idx}/{len(parsed_decisions)}]")
                    logger.info(f"  Symbol: {symbol}")
                    logger.info(f"  Action: {action}")
                    logger.info(f"  Confidence: {confidence:.2f}")
                    logger.info(f"  Reason: {reason[:100]}...")

                    # Check if symbol was recently closed
                    if symbol and symbol in (recently_closed or []) and action in ["BUY", "SELL"]:
                        logger.warning(f"  {action} {symbol}: Recently closed (within 2h)")
                        if confidence < 0.7:
                            logger.warning(f"  REJECTED: Low confidence ({confidence:.2f}) on recently closed symbol")
                            continue
                        else:
                            logger.info(f"  ALLOWED: High confidence ({confidence:.2f}) overrides recent close")

                    # Check if already have position - if opposite direction, convert to CLOSE
                    existing_pos = next((p for p in current_positions if p.get('symbol') == symbol), None)
                    if existing_pos and action in ["BUY", "SELL"]:
                        pos_side = existing_pos.get('side', existing_pos.get('direction', 'LONG'))
                        is_long = pos_side.upper() in ['LONG', 'BUY']

                        # If LLM says opposite direction, convert to CLOSE instead of rejecting
                        if (is_long and action == "SELL") or (not is_long and action == "BUY"):
                            logger.info(f"  🔄 Converting {action} to CLOSE: LLM wants opposite direction")
                            action = "CLOSE"
                            parsed["action"] = "CLOSE"
                        else:
                            # Same direction = trying to add to position, reject
                            logger.info(f"  REJECTED: Already have {pos_side} position in {symbol}")
                            continue

                    # Validate symbol is an Extended market
                    if symbol not in self.aggregator.extended_markets:
                        logger.warning(f"  REJECTED: {symbol} is not in Extended whitelist")
                        logger.warning(f"  Available: {', '.join(sorted(self.aggregator.extended_markets))}")
                        continue
                    else:
                        logger.info(f"  Symbol validation passed - {symbol} is available on Extended")

                    # Validate decision
                    is_valid, error = self.llm_agent.response_parser.validate_decision(
                        parsed,
                        open_positions=current_positions,
                        max_positions=self.llm_agent.max_positions
                    )

                    if is_valid:
                        logger.info(f"  ACCEPTED: {action} {symbol} validated successfully")
                        valid_decisions.append({
                            "action": action,
                            "symbol": symbol,
                            "reasoning": parsed.get("reason", ""),
                            "confidence": parsed.get("confidence", 0.5),
                            "cost": result.get("cost", 0)
                        })
                        # Track position if opening new
                        if action in ["BUY", "SELL"]:
                            current_positions.append({"symbol": symbol})
                    else:
                        logger.warning(f"  REJECTED: {error}")

                # Log validation summary
                logger.info("")
                logger.info("=" * 80)
                logger.info(f"VALIDATION SUMMARY:")
                logger.info(f"  Total decisions from LLM: {len(parsed_decisions)}")
                logger.info(f"  Passed validation: {len(valid_decisions)}")
                logger.info(f"  Failed validation: {len(parsed_decisions) - len(valid_decisions)}")
                logger.info("=" * 80)
                logger.info("")

                if valid_decisions:
                    decisions = valid_decisions
                    break
                else:
                    if parsed_decisions:
                        logger.warning(f"All {len(parsed_decisions)} decisions failed validation")
                    if attempt < self.llm_agent.max_retries:
                        continue
            else:
                # All retries failed
                decisions = None

            if not decisions:
                logger.warning("No valid decisions from LLM")
                return

            # Handle multiple decisions
            if not isinstance(decisions, list):
                decisions = [decisions]

            # Log decisions
            total_cost = sum(d.get('cost', 0) for d in decisions)
            logger.info("")
            logger.info(f"LLM DECISIONS: {len(decisions)} decision(s) | Cost: ${total_cost:.4f}")

            # Execute each decision
            for i, decision in enumerate(decisions, 1):
                action = decision.get('action')
                symbol = decision.get('symbol', 'N/A')

                # Map BUY/SELL to LONG/SHORT for executor
                if action == "BUY":
                    decision['action'] = "LONG"
                    action = "LONG"
                elif action == "SELL":
                    decision['action'] = "SHORT"
                    action = "SHORT"

                logger.info(f"\n[{i}/{len(decisions)}] {action} {symbol}")
                logger.info(f"   Reasoning: {decision.get('reasoning', 'N/A')}")

                # TREND FILTER (2025-12-02): Block shorts in bullish trends
                if action == "SHORT":
                    # Get market data for this symbol
                    symbol_data = market_data_dict.get(symbol, {})
                    sma20 = symbol_data.get('sma20')
                    sma50 = symbol_data.get('sma50')
                    current_price = symbol_data.get('price')

                    if sma20 and sma50 and current_price:
                        if sma20 > sma50 and current_price > sma20:
                            logger.warning(f"  ⛔ REJECTED: Cannot SHORT {symbol} in bullish trend")
                            logger.warning(f"     SMA20 ({sma20:.2f}) > SMA50 ({sma50:.2f}), Price ({current_price:.2f}) > SMA20")
                            logger.warning(f"     Trend filter protecting from bad short")
                            continue  # Skip this decision

                result = await self.executor.execute_decision(decision)

                if result.get('success'):
                    logger.info(f"   Execution successful")
                else:
                    logger.warning(f"   Execution failed: {result.get('error', 'Unknown')}")

                # Track decision
                self.decision_history.append({
                    'timestamp': current_time,
                    'decision': decision,
                    'result': result
                })

        except Exception as e:
            logger.error(f"Error in decision cycle: {e}", exc_info=True)

        logger.info("=" * 80)
        logger.info("Decision cycle complete")
        logger.info("=" * 80)

    async def run(self):
        """Run continuous trading loop with fast exit monitoring"""
        logger.info("Starting Extended trading bot...")
        logger.info(f"   Mode: {['LIVE', 'DRY-RUN'][self.dry_run]}")
        logger.info(f"   Strategy: {self.strategy}")
        logger.info(f"   Check Interval: {self.check_interval}s")
        logger.info(f"   Position Size: ${self.position_size}")

        cycle_count = 0

        # Start fast exit monitor as background task (only for strategies with exit rules)
        if self.exit_rules and self.strategy not in ["D", "E"]:
            self.fast_exit_task = asyncio.create_task(self.fast_exit_monitor.run())
            logger.info("Fast exit monitor started (30s price checks + trailing)")
        else:
            logger.info("Fast exit monitor disabled for pairs strategies")

        try:
            while True:
                cycle_count += 1
                logger.info(f"\n{'='*80}")
                logger.info(f"Cycle {cycle_count} - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                logger.info(f"{'='*80}")

                # Log fast exit stats periodically (if enabled)
                if cycle_count % 6 == 0 and self.exit_rules:  # Every 30 minutes
                    self.fast_exit_monitor.log_stats()

                await self.run_once()

                logger.info(f"Waiting {self.check_interval}s until next cycle...")
                await asyncio.sleep(self.check_interval)

        except KeyboardInterrupt:
            logger.info("\nShutting down gracefully...")
            if self.exit_rules and hasattr(self, 'fast_exit_monitor'):
                self.fast_exit_monitor.stop()
            if self.fast_exit_task:
                self.fast_exit_task.cancel()
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            if self.exit_rules and hasattr(self, 'fast_exit_monitor'):
                self.fast_exit_monitor.stop()
            if self.fast_exit_task:
                self.fast_exit_task.cancel()
        finally:
            # Close SDK client
            try:
                await self.executor.client.close()
            except Exception:
                pass


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Extended Trading Bot (Starknet Perps)")
    parser.add_argument('--dry-run', action='store_true', help='Dry run mode (no real trades)')
    parser.add_argument('--live', action='store_true', help='Live trading mode')
    parser.add_argument('--once', action='store_true', help='Run once and exit')
    parser.add_argument('--interval', type=int, default=300, help='Check interval in seconds (default: 300)')
    parser.add_argument('--model', type=str, default='qwen-max',
                        choices=['deepseek-chat', 'qwen-max'],
                        help='LLM model to use (default: qwen-max = Alpha Arena winner)')
    parser.add_argument('--strategy', type=str, default='B',
                        choices=['B', 'C', 'D', 'E'],
                        help='Strategy: B=LLM-based, C=Copy whale, D=Pairs trade, E=Self-improving pairs (default: B)')

    args = parser.parse_args()

    # Validate arguments
    if not args.dry_run and not args.live:
        logger.error("Must specify either --dry-run or --live")
        sys.exit(1)

    dry_run = not args.live

    # Load environment variables
    cambrian_api_key = os.getenv('CAMBRIAN_API_KEY')
    deepseek_api_key = os.getenv('DEEPSEEK_API_KEY')
    openrouter_api_key = os.getenv('OPEN_ROUTER')

    # Determine which LLM API key to use based on model choice
    model = args.model
    if model == 'qwen-max':
        llm_api_key = openrouter_api_key
        if not llm_api_key:
            logger.error("OPEN_ROUTER env var required for qwen-max model")
            sys.exit(1)
        logger.info(f"Using Qwen-Max via OpenRouter")
    else:
        llm_api_key = deepseek_api_key
        if not llm_api_key:
            logger.error("DEEPSEEK_API_KEY env var required for deepseek-chat model")
            sys.exit(1)
        logger.info(f"Using DeepSeek-Chat")

    # Validate Extended credentials
    extended_api_key = os.getenv('EXTENDED_API_KEY')
    extended_private = os.getenv('EXTENDED_STARK_PRIVATE_KEY')
    extended_public = os.getenv('EXTENDED_STARK_PUBLIC_KEY')
    extended_vault = os.getenv('EXTENDED_VAULT')

    missing = []
    if not cambrian_api_key:
        missing.append('CAMBRIAN_API_KEY')
    if not extended_api_key:
        missing.append('EXTENDED_API_KEY')
    if not extended_private:
        missing.append('EXTENDED_STARK_PRIVATE_KEY')
    if not extended_public:
        missing.append('EXTENDED_STARK_PUBLIC_KEY')
    if not extended_vault:
        missing.append('EXTENDED_VAULT')

    if missing:
        logger.error(f"Missing environment variables: {', '.join(missing)}")
        logger.error("Get Extended credentials from: https://app.extended.exchange/api-management")
        sys.exit(1)

    # Create bot
    bot = ExtendedTradingBot(
        cambrian_api_key=cambrian_api_key,
        deepseek_api_key=llm_api_key,
        dry_run=dry_run,
        check_interval=args.interval,
        model=model,
        strategy=args.strategy
    )

    # Run bot
    if args.once:
        logger.info("Running single cycle...")
        asyncio.run(bot.run_once())
        logger.info("Single cycle complete")
    else:
        asyncio.run(bot.run())


if __name__ == "__main__":
    main()
