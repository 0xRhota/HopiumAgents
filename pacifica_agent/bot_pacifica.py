#!/usr/bin/env python3
"""
Pacifica Trading Bot - Main Entry Point
Mirrors Pacifica bot structure, adapted for Pacifica DEX

Usage:
    python -m pacifica_agent.bot_pacifica --dry-run
    python -m pacifica_agent.bot_pacifica --live
    python -m pacifica_agent.bot_pacifica --dry-run --once
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

# Reuse Pacifica bot's LLM system (same structure)
from llm_agent.llm import LLMTradingAgent
from trade_tracker import TradeTracker
from dexes.pacifica.pacifica_sdk import PacificaSDK
from pacifica_agent.execution.pacifica_executor import PacificaTradeExecutor
from pacifica_agent.data.pacifica_aggregator import PacificaMarketDataAggregator

# Load environment variables from project root
project_root_env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
load_dotenv(dotenv_path=project_root_env, override=True)

# Configure clean, human-readable logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s',
    handlers=[
        logging.FileHandler('logs/pacifica_bot.log'),
        logging.StreamHandler()
    ]
)

# Suppress noisy loggers
logging.getLogger('pacifica_agent.data.pacifica_fetcher').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.ERROR)
logging.getLogger('asyncio').setLevel(logging.ERROR)
logging.getLogger('dexes.pacifica.pacifica_sdk').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info(f"✅ Loaded environment variables from: {project_root_env}")


class PacificaTradingBot:
    """Pacifica trading bot - mirrors Pacifica bot structure"""

    def __init__(
        self,
        cambrian_api_key: str,
        deepseek_api_key: str,
        pacifica_private_key: str,
        pacifica_account: str,
        dry_run: bool = True,
        check_interval: int = 300,  # 5 minutes (frequent monitoring for swing positions)
        position_size: float = 250.0,  # $250 notional = $5 margin @ 50x leverage (matches Lighter)
        max_positions: int = 15,
        use_sentiment_filter: bool = False,  # Deep42 sentiment filtering (OFF by default)
        sentiment_threshold_bullish: float = 60.0,  # Min bullish % for longs
        sentiment_threshold_bearish: float = 40.0,  # Min bearish % for shorts
        min_confidence: float = 0.7,  # Minimum confidence threshold for swings (0.7 for high-conviction only)
        max_position_age_minutes: int = 2880,  # 48 hours (swing strategy hold time)
        model: str = "deepseek-chat",  # LLM model (deepseek-chat or qwen-max)
        prompt_version: str = "v1_original"  # Prompt strategy version
    ):
        """
        Initialize Pacifica trading bot

        Args:
            cambrian_api_key: Cambrian API key for macro context
            deepseek_api_key: DeepSeek API key for LLM decisions
            pacifica_private_key: Pacifica API private key
            pacifica_account: Pacifica account address
            dry_run: If True, simulate trades without execution
            check_interval: Seconds between decision checks (default: 300 = 5 min for frequent monitoring)
            position_size: USD per trade (default: $5)
            max_positions: Max open positions (default: 15)
            use_sentiment_filter: Enable Deep42 sentiment filtering (default: False)
            sentiment_threshold_bullish: Min bullish % for longs (default: 60%)
            sentiment_threshold_threshold_bearish: Min bearish % for shorts (default: 40%)
            min_confidence: Minimum confidence threshold for LLM decisions (default: 0.7 for swings)
            max_position_age_minutes: Maximum position age in minutes before auto-close (default: 2880 = 48 hours)
            model: LLM model to use (deepseek-chat or qwen-max)
            prompt_version: Prompt strategy version (v1_original, v7_alpha_arena, v8_pure_pnl, v9_qwen_enhanced)
        """
        self.dry_run = dry_run
        self.check_interval = check_interval
        self.cambrian_api_key = cambrian_api_key
        self.min_confidence = min_confidence
        self.prompt_version = prompt_version  # Store for use in run_once

        logger.info(f"Initializing Pacifica Trading Bot ({['LIVE', 'DRY-RUN'][dry_run]} mode)")
        logger.info(f"Minimum confidence threshold: {min_confidence:.2f}")
        logger.info(f"🤖 Model: {model}")
        logger.info(f"📝 Prompt Strategy: {prompt_version}")

        # Initialize Pacifica data aggregator (uses Pacifica API, not Pacifica!)
        self.aggregator = PacificaMarketDataAggregator(
            cambrian_api_key=cambrian_api_key,
            interval="15m",
            candle_limit=100,
            macro_refresh_hours=12
        )
        logger.info("✅ Using Pacifica DEX data (not Pacifica)")

        # Initialize LLM agent with model and prompt_strategy
        self.llm_agent = LLMTradingAgent(
            deepseek_api_key=deepseek_api_key,
            cambrian_api_key=cambrian_api_key,
            model=model,
            max_retries=2,
            daily_spend_limit=10.0,  # Shared with Pacifica bot
            max_positions=max_positions,
            prompt_strategy=prompt_version  # Maps to prompt_strategy parameter
        )

        # Store SDK initialization params (will initialize in async context)
        self.pacifica_private_key = pacifica_private_key
        self.pacifica_account = pacifica_account
        self.pacifica_sdk = None  # Will be initialized in async context

        self.trade_tracker = TradeTracker(dex="pacifica")

        # Store executor params (will initialize after SDK)
        self.executor = None
        self._executor_params = {
            "trade_tracker": self.trade_tracker,
            "dry_run": dry_run,
            "default_position_size": position_size,
            "max_positions": max_positions,
            "cambrian_api_key": cambrian_api_key,
            "use_sentiment_filter": use_sentiment_filter,
            "sentiment_threshold_bullish": sentiment_threshold_bullish,
            "sentiment_threshold_bearish": sentiment_threshold_bearish,
            "max_position_age_minutes": max_position_age_minutes
        }

        # Track last deep research cycle (hourly - same as Pacifica)
        self.last_deep_research_time = datetime.now()
        self.deep_research_interval = 3600  # 1 hour

        # Track decisions for hourly review
        self.decision_history = []
        
        # Store position size for logging
        self.position_size = position_size

        logger.info("✅ Pacifica Trading Bot initialized successfully")
        
        # Log prompt version
        prompt_version = self.llm_agent.prompt_formatter.get_prompt_version()
        logger.info(f"📝 Active Prompt Version: {prompt_version}")

    def _generate_hourly_review(self) -> Optional[str]:
        """Generate hourly deep research review (same as Pacifica bot)"""
        # TODO: Implement same as Pacifica bot's _generate_hourly_review
        return None


    async def _ensure_sdk_initialized(self):
        """Initialize SDK in async context (lazy initialization)"""
        if self.pacifica_sdk is None:
            logger.info("Initializing Pacifica SDK...")
            self.pacifica_sdk = PacificaSDK(
                private_key=self.pacifica_private_key,
                account_address=self.pacifica_account
            )

            # CRITICAL: Set SDK reference in data fetcher for dynamic symbol loading
            self.aggregator.pacifica.sdk = self.pacifica_sdk

            # PacificaDataFetcher uses HTTP API directly - no need for SDK API objects
            # candlestick_api and funding_api are not used (Pacifica uses HTTP API)
            self.aggregator.candlestick_api = None
            self.aggregator.funding_api = None

            # Initialize symbols eagerly now that SDK is available
            await self.aggregator.pacifica._initialize_symbols()

            # Initialize executor now that SDK is ready
            if self.executor is None:
                self.executor = PacificaTradeExecutor(
                    pacifica_sdk=self.pacifica_sdk,
                    **self._executor_params
                )
            logger.info("✅ Pacifica SDK initialized")

    async def run_once(self):
        """Run single decision cycle - mirrors Pacifica bot structure"""
        # Ensure SDK is initialized (lazy initialization in async context)
        await self._ensure_sdk_initialized()
        
        current_time = datetime.now()
        
        # Check if it's time for hourly deep research cycle
        time_since_last_research = (current_time - self.last_deep_research_time).total_seconds()
        is_deep_research_cycle = time_since_last_research >= self.deep_research_interval

        # Clean, scannable cycle header with clear START marker
        prompt_version = self.llm_agent.prompt_formatter.get_prompt_version()
        version_label = "V2" if prompt_version == "v2_deep_reasoning" else "V1"

        logger.info("")
        logger.info("")
        logger.info("╔" + "═" * 78 + "╗")
        if is_deep_research_cycle:
            logger.info(f"║ 🔬 DEEP RESEARCH CYCLE START | {current_time.strftime('%Y-%m-%d %H:%M:%S')} | {version_label:<37} ║")
        else:
            logger.info(f"║ 🤖 DECISION CYCLE START | {current_time.strftime('%Y-%m-%d %H:%M:%S')} | {version_label:<43} ║")
        logger.info("╚" + "═" * 78 + "╝")
        logger.info("")

        try:

            # Get ALL positions from Pacifica API (for reference)
            # Note: Pacifica SDK's get_positions() is synchronous (uses requests.get)
            positions_result = self.pacifica_sdk.get_positions()
            all_exchange_positions = []
            if positions_result.get('success') and positions_result.get('data'):
                for pos in positions_result['data']:
                    symbol = pos.get('symbol', f"UNKNOWN(market_id={pos.get('market_id')})")

                    # Convert Pacifica's 'bid'/'ask' to 'LONG'/'SHORT' for LLM
                    raw_side = pos.get('side', 'bid')
                    side = 'SHORT' if raw_side == 'ask' else 'LONG'

                    all_exchange_positions.append({
                        'symbol': symbol,
                        'side': side,  # Now properly converted to LONG/SHORT
                        'entry_price': float(pos.get('entry_price', 0)),
                        'current_price': float(pos.get('entry_price', 0)),
                        'size': float(pos.get('size', 0)),
                        'pnl': float(pos.get('pnl', 0)),
                        'market_id': pos.get('market_id')
                    })

            # Use ALL positions on the exchange - we manage everything
            open_positions = all_exchange_positions

            # Clean position summary with timestamp
            timestamp_str = datetime.now().strftime('%H:%M:%S')
            logger.info("")
            logger.info(f"┌─ POSITIONS ({timestamp_str}) " + "─" * 57)
            if open_positions:
                for p in open_positions:
                    # Convert to float in case API returns strings
                    entry_price = float(p['entry_price'])
                    pnl = float(p['pnl'])
                    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
                    logger.info(f"│ {pnl_emoji} {p['symbol']:<8} {p['side']:<5} ${entry_price:.4f} → P&L: ${pnl:>+7.2f}")
                logger.info(f"└─ {len(open_positions)} position(s) open")
            else:
                logger.info(f"│ No open positions")
                logger.info(f"└─ 0 positions")

            # Check for stale positions and close them (REQ-1.5: Position aging/rotation)
            logger.info("")
            closed_stale = await self.executor.check_stale_positions()
            if closed_stale:
                logger.info(f"🔄 Closed {len(closed_stale)} stale positions: {', '.join(closed_stale)}")
                # Refresh positions after closing
                positions_result = self.pacifica_sdk.get_positions()
                all_exchange_positions = []
                if positions_result.get('success') and positions_result.get('data'):
                    for pos in positions_result['data']:
                        symbol = pos.get('symbol', f"UNKNOWN(market_id={pos.get('market_id')})")
                        raw_side = pos.get('side', 'bid')
                        is_long = (raw_side == 'bid')
                        position_side = "LONG" if is_long else "SHORT"
                        all_exchange_positions.append({
                            'symbol': symbol,
                            'side': position_side,
                            'entry_price': pos.get('entry_price', 0),
                            'pnl': pos.get('pnl', 0)
                        })
                open_positions = all_exchange_positions

            # Fetch market data (condensed logging with timestamp)
            fetch_start = datetime.now()
            logger.info("")
            logger.info(f"┌─ MARKET DATA ({fetch_start.strftime('%H:%M:%S')}) " + "─" * 55)
            logger.info("│ ⏳ Fetching markets from Pacifica...")

            all_market_data_dict = await self.aggregator.fetch_all_markets()

            fetch_end = datetime.now()
            fetch_duration = (fetch_end - fetch_start).total_seconds()
            if all_market_data_dict:
                logger.info(f"│ ✅ Loaded {len(all_market_data_dict)} markets in {fetch_duration:.1f}s")
                logger.info("└─" + "─" * 76)
                logger.info("")
                # Show market summary table
                market_summary = self.aggregator.format_market_table(all_market_data_dict)
                for line in market_summary.split('\n'):
                    if line.strip():
                        logger.info(line)
            else:
                logger.info("│ ❌ No market data available")
                logger.info("└─" + "─" * 76)
            
            # Use all market data - show everything to LLM
            market_data_dict = all_market_data_dict.copy() if all_market_data_dict else {}
            
            # Convert back to list for compatibility with rest of code
            market_data_list = list(market_data_dict.values())
            
            # Only analyze Pacifica markets
            pacifica_symbols = list(market_data_dict.keys())
            logger.info(f"📊 Analyzing {len(pacifica_symbols)} Pacifica markets: {', '.join(pacifica_symbols)}")

            # Get recently closed symbols (soft warning)
            recently_closed = self.trade_tracker.get_recently_closed_symbols(hours=2)
            if recently_closed:
                logger.info(f"⚠️ Recently closed symbols (last 2h): {recently_closed} - Will allow if high confidence")

            # Generate hourly deep research review if needed
            hourly_review = None
            if is_deep_research_cycle:
                hourly_review = self._generate_hourly_review()
                if hourly_review:
                    logger.info("=" * 80)
                    logger.info("📊 HOURLY REVIEW SUMMARY:")
                    logger.info("=" * 80)
                    for line in hourly_review.split('\n')[:50]:  # Show first 50 lines
                        logger.info(f"  {line}")
                    logger.info("=" * 80)
                # Update last deep research time
                self.last_deep_research_time = current_time

            if not market_data_list:
                logger.warning("No market data available - skipping cycle")
                return

            # Get macro context only for V1 (V2 doesn't use it)
            prompt_version = self.llm_agent.prompt_formatter.get_prompt_version()
            if prompt_version == "v1_original":
                macro_context = self.aggregator.get_macro_context()
            else:
                macro_context = None  # V2 doesn't use macro context

            # Format market table (use dict, not list)
            # IMPORTANT: Only show positions we're managing in the prompt
            # Create filtered market data that excludes unmanaged positions' positions info
            market_table = self.aggregator.format_market_table(market_data_dict)

            # Get account balance from API
            try:
                balance_result = self.pacifica_sdk.get_balance()
                if balance_result.get('success') and balance_result.get('data'):
                    # Prioritize account_equity (includes unrealized P&L)
                    account_balance = float(balance_result['data'].get('account_equity', 0))
                    logger.info(f"💰 Account equity: ${account_balance:.2f} | Available: ${float(balance_result['data'].get('available_to_spend', 0)):.2f}")
                else:
                    account_balance = 0.0
                    logger.warning("Failed to fetch account balance")
            except Exception as e:
                account_balance = 0.0
                logger.warning(f"Error fetching balance: {e}")

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

            # Format prompt - V2 doesn't accept macro_context/deep42_context
            # (prompt_version already fetched above when checking macro context)

            # Build kwargs based on prompt version
            prompt_kwargs = {
                "market_table": market_table,
                "open_positions": open_positions,
                "account_balance": account_balance,
                "hourly_review": hourly_review,
                "trade_history": trade_history,
                "recently_closed_symbols": recently_closed or []
            }

            # V1 uses macro_context and deep42_context, V2 doesn't
            if prompt_version == "v1_original":
                prompt_kwargs["macro_context"] = macro_context
                prompt_kwargs["deep42_context"] = None  # NO Deep42 for Pacifica bot

            prompt = self.llm_agent.prompt_formatter.format_trading_prompt(**prompt_kwargs)

            # Get trading decision from LLM (same pattern as Pacifica bot)
            logger.info("Getting trading decision from LLM...")
            
            # Convert market_data_dict to list for validation
            all_symbols = list(market_data_dict.keys())
            
            # Call LLM with retries (same as Pacifica's get_trading_decision)
            responses = []
            for attempt in range(self.llm_agent.max_retries + 1):
                logger.info(f"LLM query attempt {attempt + 1}/{self.llm_agent.max_retries + 1}...")

                # Query model (same parameters as Pacifica)
                result = self.llm_agent.model_client.query(
                    prompt=prompt,
                    max_tokens=500,
                    temperature=0.1
                )

                if result is None:
                    logger.error(f"LLM query failed (attempt {attempt + 1})")
                    continue

                # Add response to list
                responses.append(result["content"])

                # Log full LLM response for transparency
                logger.info("")
                logger.info("=" * 80)
                logger.info("🤖 LLM RESPONSE:")
                logger.info("=" * 80)
                for line in result["content"].split('\n'):
                    logger.info(line)
                logger.info("=" * 80)
                logger.info("")

                # Try parsing multiple decisions (same parser as Pacifica)
                parsed_decisions = self.llm_agent.response_parser.parse_multiple_decisions(result["content"])
                if parsed_decisions is None or len(parsed_decisions) == 0:
                    logger.warning(f"Parse failed (attempt {attempt + 1}), will retry with clearer prompt")

                    # Modify prompt for retry (same as Pacifica)
                    if attempt < self.llm_agent.max_retries:
                        prompt += (
                            f"\n\nIMPORTANT: Analyze ALL {len(all_symbols)} markets below and respond with decisions ONLY for markets with clear trading signals:\n"
                            "TOKEN: BTC\n"
                            "DECISION: BUY BTC\n"
                            "CONFIDENCE: 0.75\n"
                            "REASON: Your reasoning here\n\n"
                            "TOKEN: SOL\n"
                            "DECISION: SELL SOL\n"
                            "CONFIDENCE: 0.65\n"
                            "REASON: Your reasoning here\n\n"
                            "Do NOT add any other text before or after."
                        )
                    continue

                # Validate all decisions (same validation as Pacifica)
                logger.info("")
                logger.info("=" * 80)
                logger.info(f"🔍 VALIDATING {len(parsed_decisions)} DECISIONS FROM LLM")
                logger.info("=" * 80)

                valid_decisions = []
                current_positions = open_positions.copy() if open_positions else []

                for idx, parsed in enumerate(parsed_decisions, 1):
                    symbol = parsed.get("symbol")
                    action = parsed.get("action", "").upper()
                    confidence = parsed.get("confidence", 0.5)
                    reason = parsed.get("reason", "No reason provided")

                    logger.info(f"\n[Decision {idx}/{len(parsed_decisions)}]")
                    logger.info(f"  Symbol: {symbol}")
                    logger.info(f"  Action: {action}")
                    logger.info(f"  Confidence: {confidence:.2f}")
                    logger.info(f"  Reason: {reason[:100]}...")  # First 100 chars

                    # NOTHING/NO_TRADE decisions are always valid (no trade executed)
                    if action == "NOTHING":
                        logger.info(f"  ✅ ACCEPTED: NO_TRADE decision - waiting for better setup")
                        valid_decisions.append({
                            "action": action,
                            "symbol": symbol,
                            "reason": parsed.get("reason", ""),
                            "confidence": parsed.get("confidence", 0.5),
                            "cost": result.get("cost", 0)
                        })
                        continue

                    # Minimum confidence filter (REQ-1.2) - only for actual trades
                    if confidence < self.min_confidence:
                        logger.warning(f"  ❌ REJECTED: Low confidence ({confidence:.2f}) - minimum required: {self.min_confidence:.2f}")
                        continue
                    else:
                        logger.info(f"  ✅ Confidence check passed: {confidence:.2f} >= {self.min_confidence:.2f}")

                    # Check if symbol was recently closed (prevent immediate re-entry)
                    # Only block if confidence is low (< 0.7) - allow high confidence trades (same as Pacifica)
                    if symbol and symbol in (recently_closed or []) and action in ["BUY", "SELL"]:
                        logger.warning(f"  ⚠️ {action} {symbol}: Recently closed (within 2h) - checking confidence")
                        # Don't block - just warn. Let the LLM's confidence decide.
                        # Only block if confidence is low (< 0.7)
                        if confidence < 0.7:
                            logger.warning(f"  ❌ REJECTED: Low confidence ({confidence:.2f}) on recently closed symbol")
                            continue
                        else:
                            logger.info(f"  ✅ ALLOWED: High confidence ({confidence:.2f}) overrides recent close")

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
                            logger.info(f"  ❌ REJECTED: Already have {pos_side} position in {symbol}")
                            continue

                    # Validate symbol is a Pacifica market (use aggregator's dynamic list from API)
                    if symbol not in self.aggregator.pacifica_markets:
                        logger.warning(f"  ❌ REJECTED: {symbol} is not a Pacifica market")
                        logger.warning(f"  Available Pacifica markets: {', '.join(sorted(self.aggregator.pacifica_markets)[:20])}... (101 total)")
                        continue
                    else:
                        logger.info(f"  ✅ Symbol validation passed - {symbol} is available on Pacifica")
                    
                    # Validate decision (same as Pacifica)
                    is_valid, error = self.llm_agent.response_parser.validate_decision(
                        parsed,
                        open_positions=current_positions,
                        max_positions=self.llm_agent.max_positions
                    )
                    
                    if is_valid:
                        logger.info(f"  ✅ ACCEPTED: {action} {symbol} validated successfully")
                        valid_decisions.append({
                            "action": action,
                            "symbol": symbol,
                            "reason": parsed.get("reason", ""),
                            "confidence": parsed.get("confidence", 0.5),
                            "cost": result.get("cost", 0)
                        })
                        # Track position if opening new
                        if action in ["BUY", "SELL"]:
                            current_positions.append({"symbol": symbol})
                    else:
                        logger.warning(f"  ❌ REJECTED: {error}")
                
                # Log validation summary
                logger.info("")
                logger.info("=" * 80)
                logger.info(f"📊 VALIDATION SUMMARY:")
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
                logger.error("Failed to get decision from LLM")
                return

            # Handle multiple decisions (same as Pacifica)
            if not isinstance(decisions, list):
                decisions = [decisions]  # Backward compatibility

            # Clean LLM decision output with FULL reasoning (not truncated)
            prompt_version = self.llm_agent.prompt_formatter.get_prompt_version()
            total_cost = sum(d.get('cost', 0) for d in decisions)
            llm_timestamp = datetime.now().strftime('%H:%M:%S')

            logger.info("")
            logger.info(f"┌─ LLM DECISIONS ({llm_timestamp}) " + "─" * 52)
            logger.info(f"│ 📋 {len(decisions)} decision(s) | Version: {version_label} | Cost: ${total_cost:.4f}")
            logger.info("├" + "─" * 77)

            # Track decisions for hourly review
            current_time_str = datetime.now().isoformat()

            for i, decision in enumerate(decisions, 1):
                action_emoji = {"BUY": "🟢", "SELL": "🔴", "CLOSE": "🔵", "NOTHING": "⚪"}.get(decision['action'], "❓")
                symbol = decision.get('symbol') or 'N/A'  # Handle None explicitly
                confidence = decision.get('confidence') or 0.5  # Handle None explicitly
                # Show FULL reasoning (not truncated) - break into multiple lines if needed
                full_reason = (decision.get('reason') or '').strip()

                logger.info(f"│ {action_emoji} [{i}] {decision['action']:<8} {symbol:<8} @{confidence:.2f}")

                # Format reasoning with line breaks and indentation
                if full_reason:
                    # Split long reasoning into multiple lines (70 chars per line)
                    reason_lines = []
                    words = full_reason.split()
                    current_line = ""
                    for word in words:
                        if len(current_line) + len(word) + 1 <= 70:
                            current_line += (" " if current_line else "") + word
                        else:
                            if current_line:
                                reason_lines.append(current_line)
                            current_line = word
                    if current_line:
                        reason_lines.append(current_line)

                    # Log each line with proper indentation
                    for line in reason_lines:
                        logger.info(f"│     {line}")
                
                # Store decision for hourly review
                decision_record = {
                    'timestamp': current_time_str,
                    'action': decision.get('action', 'UNKNOWN'),
                    'symbol': decision.get('symbol'),
                    'side': decision.get('side'),
                    'confidence': decision.get('confidence', 0.5),
                    'reason': decision.get('reason', ''),
                    'position_size': decision.get('position_size'),
                    'open_positions_count': len(open_positions),
                    'market_data_summary': f"{len(market_data_list)} markets",
                    'executed': False,
                    'execution_result': None
                }
                self.decision_history.append(decision_record)

            logger.info("└─" + "─" * 76)

            # Execute decisions (clean output)
            logger.info("")
            logger.info("┌─ EXECUTION " + "─" * 64)

            decision_idx = len(self.decision_history) - len(decisions)
            for i, decision in enumerate(decisions, 1):
                action_emoji = {"BUY": "🟢", "SELL": "🔴", "CLOSE": "🔵", "NOTHING": "⚪"}.get(decision['action'], "❓")
                symbol = decision.get('symbol', 'N/A')

                if decision['action'] == "NOTHING":
                    logger.info(f"│ ⚪ [{i}] NOTHING - No action taken")
                    if decision_idx < len(self.decision_history):
                        self.decision_history[decision_idx]['executed'] = True
                        self.decision_history[decision_idx]['execution_result'] = {'success': True, 'action': 'NOTHING'}
                    decision_idx += 1
                    continue

                logger.info(f"│ {action_emoji} [{i}] {decision['action']:<8} {symbol:<8} → Executing...")

                # Inject current_price from market data into decision
                if symbol and symbol in market_data_dict:
                    decision['current_price'] = market_data_dict[symbol].get('price')

                # Add delay between orders to prevent nonce conflicts
                if i > 1:
                    await asyncio.sleep(2.0)

                # Execute via Pacifica executor (async)
                result = await self.executor.execute_decision(decision)

                # Update decision record with execution result
                if decision_idx < len(self.decision_history):
                    self.decision_history[decision_idx]['executed'] = True
                    self.decision_history[decision_idx]['execution_result'] = result
                decision_idx += 1

                if result.get('success'):
                    logger.info(f"│     ✅ Success: {result['action']} {symbol}")
                else:
                    logger.info(f"│     ❌ Failed: {result.get('error', 'Unknown error')}")

            logger.info("└─" + "─" * 76)

            # Cycle summary with completion timestamp
            cycle_end = datetime.now()
            cycle_duration = (cycle_end - current_time).total_seconds()
            logger.info("")
            logger.info("╔" + "═" * 78 + "╗")
            logger.info(f"║ ✅ CYCLE COMPLETE ({cycle_end.strftime('%H:%M:%S')}) | Duration: {cycle_duration:.1f}s | Cost: ${total_cost:.4f} | Daily: ${self.llm_agent.get_daily_spend():.4f}/$10 ║")
            logger.info("╚" + "═" * 78 + "╝")
            logger.info("")

        except Exception as e:
            logger.error("")
            logger.error("╔" + "═" * 78 + "╗")
            logger.error(f"║ ❌ CYCLE ERROR: {str(e):<65} ║")
            logger.error("╚" + "═" * 78 + "╝")
            logger.error("", exc_info=True)

    async def run(self):
        """Main bot loop - mirrors Pacifica bot"""
        logger.info("Starting Pacifica Trading Bot main loop")
        logger.info(f"Check interval: {self.check_interval} seconds ({self.check_interval // 60} minutes)")
        
        prompt_version = self.llm_agent.prompt_formatter.get_prompt_version()
        logger.info(f"📝 Active Prompt Version: {prompt_version}")
        logger.info(f"Position size: ${self.position_size}")

        try:
            while True:
                await self.run_once()

                # Calculate and display next cycle time
                next_cycle_time = datetime.now() + timedelta(seconds=self.check_interval)
                logger.info("")
                logger.info("╔" + "═" * 78 + "╗")
                logger.info(f"║ ⏰ NEXT CYCLE AT: {next_cycle_time.strftime('%Y-%m-%d %H:%M:%S')} (in {self.check_interval}s / {self.check_interval//60}min){'':>16} ║")
                logger.info("╚" + "═" * 78 + "╝")
                logger.info("")

                await asyncio.sleep(self.check_interval)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user (Ctrl+C)")
        except Exception as e:
            logger.error(f"Bot crashed: {e}", exc_info=True)


def main():
    """Main entry point"""

    parser = argparse.ArgumentParser(description="Pacifica Trading Bot")
    parser.add_argument("--live", action="store_true", help="Enable live trading (default: dry-run)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Dry run mode (no real trades)")
    parser.add_argument("--once", action="store_true", help="Run once and exit (for testing)")
    parser.add_argument("--interval", type=int, default=300, help="Check interval in seconds (default: 300 = 5 min)")
    parser.add_argument("--model", type=str, default="deepseek-chat",
                        choices=["deepseek-chat", "qwen-max"],
                        help="LLM model (default: deepseek-chat, qwen-max = Alpha Arena winner)")
    parser.add_argument("--prompt", type=str, default="v1_original",
                        choices=["v1_original", "v7_alpha_arena", "v8_pure_pnl", "v9_qwen_enhanced"],
                        help="Prompt strategy (default: v1_original, v9_qwen_enhanced = Qwen scoring + funding zones)")

    # Sentiment filter arguments
    parser.add_argument("--use-sentiment-filter", action="store_true", help="Enable Deep42 sentiment filtering")
    parser.add_argument("--sentiment-bullish", type=float, default=60.0, help="Min bullish %% for longs (default: 60)")
    parser.add_argument("--sentiment-bearish", type=float, default=40.0, help="Min bearish %% for shorts (default: 40)")

    # Confidence threshold argument
    parser.add_argument("--min-confidence", type=float, default=0.7, help="Minimum confidence threshold for swings (default: 0.7)")

    # Position aging argument (REQ-1.5)
    parser.add_argument("--max-position-age", type=int, default=2880, help="Maximum position age in minutes before auto-close (default: 2880 = 48 hours)")

    args = parser.parse_args()

    # Determine mode
    if args.live:
        dry_run = False
        logger.warning("⚠️  LIVE TRADING MODE ENABLED ⚠️")
    else:
        dry_run = True
        logger.info("✅ Dry-run mode (no real trades)")

    # Get API keys from environment
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    load_dotenv(dotenv_path=env_path, override=True)

    cambrian_api_key = os.getenv("CAMBRIAN_API_KEY")
    deepseek_api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_KEY")
    openrouter_api_key = os.getenv("OPEN_ROUTER")
    pacifica_private_key = os.getenv("PACIFICA_API_KEY") or os.getenv("PACIFICA_PRIVATE_KEY") or os.getenv("PACIFICA_API_KEY_PRIVATE")
    pacifica_account = os.getenv("PACIFICA_ACCOUNT")

    # Determine which API key to use based on model
    model = args.model
    if model == "qwen-max":
        llm_api_key = openrouter_api_key
        if not llm_api_key:
            logger.error("❌ OPEN_ROUTER env var required for qwen-max model")
            sys.exit(1)
        logger.info(f"🤖 Using Qwen-Max via OpenRouter (Alpha Arena winner!)")
    else:
        llm_api_key = deepseek_api_key
        if not llm_api_key:
            logger.error("❌ DEEPSEEK_API_KEY env var required for deepseek-chat model")
            sys.exit(1)
        logger.info(f"🤖 Using DeepSeek-Chat")

    if not all([cambrian_api_key, pacifica_private_key, pacifica_account]):
        logger.error("Missing required environment variables:")
        logger.error("  - CAMBRIAN_API_KEY")
        logger.error("  - PACIFICA_API_KEY (or PACIFICA_PRIVATE_KEY)")
        logger.error("  - PACIFICA_ACCOUNT")
        sys.exit(1)

    # Initialize bot
    bot = PacificaTradingBot(
        cambrian_api_key=cambrian_api_key,
        deepseek_api_key=llm_api_key,  # Now supports either DeepSeek or OpenRouter key
        pacifica_private_key=pacifica_private_key,
        pacifica_account=pacifica_account,
        dry_run=dry_run,
        check_interval=args.interval,
        use_sentiment_filter=args.use_sentiment_filter,
        sentiment_threshold_bullish=args.sentiment_bullish,
        sentiment_threshold_bearish=args.sentiment_bearish,
        min_confidence=args.min_confidence,
        max_position_age_minutes=args.max_position_age,
        model=model,
        prompt_version=args.prompt
    )

    # Run
    if args.once:
        logger.info("Running single decision cycle...")
        asyncio.run(bot.run_once())
    else:
        asyncio.run(bot.run())


if __name__ == "__main__":
    main()

