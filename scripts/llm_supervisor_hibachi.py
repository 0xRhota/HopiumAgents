#!/usr/bin/env python3
"""
LLM Directional Supervisor for Hibachi DEX
Runs alongside Grid MM, trades ETH/SOL based on v9 scoring

Key Features:
- 10-minute LLM check intervals
- v9 scoring system (score >= 3.0 to trade)
- Trades only ETH/USDT-P and SOL/USDT-P (BTC reserved for Grid MM)
- Dynamic sizing based on available margin (5-10x leverage)
- 30-second fast exit monitoring (FREE - no LLM)
- Qwen via OpenRouter (Alpha Arena winner)

Architecture:
- Runs as separate process from Grid MM
- Coordinates via exchange API (reads positions from Hibachi)
- No state sharing needed - both scripts read from exchange
"""

import os
import sys
import asyncio
import logging
import argparse
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List

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
from llm_agent.llm.model_client import ModelClient
from hibachi_agent.data.hibachi_aggregator import HibachiMarketDataAggregator


class HibachiLLMSupervisor:
    """
    LLM Directional Supervisor for Hibachi DEX
    Trades ETH/SOL while Grid MM handles BTC
    """

    # Asset isolation - HARDCODED
    ALLOWED_SYMBOLS = {"ETH/USDT-P", "SOL/USDT-P"}
    FORBIDDEN_SYMBOLS = {"BTC/USDT-P"}  # Reserved for Grid MM

    # Parameters
    CHECK_INTERVAL = 600  # 10 minutes
    FAST_EXIT_INTERVAL = 30  # 30 seconds
    SCORE_THRESHOLD = 3.0
    TP_PCT = 8.0
    SL_PCT = 4.0
    MAX_HOLD_HOURS = 4
    MIN_LEVERAGE = 5.0
    MAX_LEVERAGE = 10.0
    MARGIN_BUFFER = 20.0  # Reserve for Grid MM

    def __init__(self, dry_run: bool = True):
        """
        Initialize supervisor

        Args:
            dry_run: If True, log trades but don't execute
        """
        self.dry_run = dry_run

        # SDK
        self.sdk: Optional[HibachiSDK] = None

        # Market data aggregator (for real indicators)
        self.aggregator: Optional[HibachiMarketDataAggregator] = None

        # LLM client
        self.llm_client: Optional[ModelClient] = None

        # Position tracking
        self.open_position: Optional[Dict] = None
        self.entry_time: Optional[datetime] = None
        self.entry_price: Optional[float] = None

        # Stats
        self.cycles_run = 0
        self.trades_made = 0
        self.total_pnl = 0.0
        self.start_time = None

        # Fast exit monitor
        self.fast_exit_running = False

    async def initialize(self):
        """Initialize SDK and LLM client"""
        logger.info("=" * 70)
        logger.info("LLM DIRECTIONAL SUPERVISOR - HIBACHI")
        logger.info("=" * 70)
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"Allowed Symbols: {self.ALLOWED_SYMBOLS}")
        logger.info(f"Check Interval: {self.CHECK_INTERVAL}s (10 min)")
        logger.info(f"Score Threshold: {self.SCORE_THRESHOLD}/5.0")
        logger.info(f"TP/SL: +{self.TP_PCT}% / -{self.SL_PCT}%")
        logger.info(f"Max Hold: {self.MAX_HOLD_HOURS} hours")
        logger.info(f"Leverage Range: {self.MIN_LEVERAGE}x - {self.MAX_LEVERAGE}x")
        logger.info("=" * 70)

        # Initialize Hibachi SDK
        api_key = os.getenv('HIBACHI_PUBLIC_KEY')
        api_secret = os.getenv('HIBACHI_PRIVATE_KEY')
        account_id = os.getenv('HIBACHI_ACCOUNT_ID')

        if not api_key or not api_secret or not account_id:
            raise ValueError("HIBACHI_PUBLIC_KEY, HIBACHI_PRIVATE_KEY, HIBACHI_ACCOUNT_ID required")

        self.sdk = HibachiSDK(api_key, api_secret, account_id)

        # Initialize market data aggregator for real indicators
        cambrian_key = os.getenv('CAMBRIAN_API_KEY', '')
        self.aggregator = HibachiMarketDataAggregator(
            cambrian_api_key=cambrian_key,
            sdk=self.sdk,
            interval="5m",
            candle_limit=100
        )
        # Initialize symbols from Hibachi
        await self.aggregator.hibachi._initialize_symbols()
        logger.info(f"Market data aggregator initialized with {len(self.aggregator.hibachi.available_symbols)} symbols")

        # Initialize LLM client (Qwen via OpenRouter)
        openrouter_key = os.getenv('OPEN_ROUTER')
        if not openrouter_key:
            raise ValueError("OPEN_ROUTER API key required for Qwen")

        self.llm_client = ModelClient(
            api_key=openrouter_key,
            model="qwen-max",
            daily_spend_limit=5.0  # $5/day max
        )

        # Get initial balance
        balance = await self.sdk.get_balance()
        logger.info(f"Account balance: ${balance:.2f}" if balance else "Balance fetch failed")

        # Check for existing supervisor positions
        await self._check_existing_positions()

        self.start_time = datetime.now()
        logger.info("Supervisor initialized successfully")
        return True

    async def _check_existing_positions(self):
        """Check for existing ETH/SOL positions on startup"""
        positions = await self.sdk.get_positions()
        for pos in positions:
            symbol = pos.get('symbol')
            if symbol in self.ALLOWED_SYMBOLS:
                qty = float(pos.get('quantity', 0))
                if qty != 0:
                    direction = pos.get('direction', 'Long')
                    entry = float(pos.get('entryPrice', 0))
                    self.open_position = {
                        'symbol': symbol,
                        'direction': direction,
                        'quantity': qty,
                        'entry_price': entry
                    }
                    self.entry_price = entry
                    self.entry_time = datetime.now()  # Approximate
                    logger.info(f"Found existing position: {direction} {qty} {symbol} @ ${entry:.2f}")
                    break

    async def get_available_margin(self) -> float:
        """Get margin available for supervisor trades"""
        # Use get_balance() which is known to work
        balance = await self.sdk.get_balance()
        if not balance:
            return 0.0

        # Reserve buffer for Grid MM
        usable = max(0, balance - self.MARGIN_BUFFER)
        return usable

    async def get_market_data(self, symbol: str) -> Optional[Dict]:
        """
        Fetch market data for scoring using HibachiMarketDataAggregator

        Returns dict with: price, rsi, macd, volume, funding
        """
        try:
            # Use aggregator for real indicator data
            data = await self.aggregator.fetch_market_data(symbol)
            if not data:
                logger.warning(f"No aggregator data for {symbol}")
                return None

            indicators = data.get('indicators', {})
            price = data.get('price') or indicators.get('price', 0)
            funding = data.get('funding_rate', 0) or 0

            # Extract RSI and MACD from indicators
            rsi = indicators.get('rsi', 50.0)
            macd = indicators.get('macd', 0.0)
            macd_signal = indicators.get('macd_signal', 0.0)

            # Calculate volume ratio (current vs average)
            # Volume ratio > 1.0 means higher than average
            volume_ratio = 1.0  # Default
            kline_df = data.get('kline_df')
            if kline_df is not None and len(kline_df) > 20:
                recent_vol = kline_df['volume'].iloc[-1]
                avg_vol = kline_df['volume'].iloc[-20:].mean()
                if avg_vol > 0:
                    volume_ratio = recent_vol / avg_vol

            # OI change (if available)
            oi_data = data.get('oi')
            oi_change_pct = 0.0
            if oi_data and isinstance(oi_data, dict):
                oi_change_pct = oi_data.get('change_pct', 0.0)

            logger.info(f"  {symbol}: RSI={rsi:.1f}, MACD={macd:.4f}, Vol={volume_ratio:.2f}x, Funding={funding:.4f}%")

            return {
                'symbol': symbol,
                'price': price,
                'rsi': rsi if rsi else 50.0,
                'macd': macd if macd else 0.0,
                'macd_signal': macd_signal if macd_signal else 0.0,
                'volume_ratio': volume_ratio,
                'funding': funding,
                'oi_change_pct': oi_change_pct
            }
        except Exception as e:
            logger.error(f"Error fetching market data for {symbol}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _build_scoring_prompt(self, eth_data: Dict, sol_data: Dict) -> str:
        """Build prompt for v9 scoring"""
        return f"""You are a trading signal scorer. Score each asset using the v9 system.

MARKET DATA:

ETH/USDT-P:
- Price: ${eth_data.get('price', 0):.2f}
- RSI: {eth_data.get('rsi', 50):.1f}
- MACD: {eth_data.get('macd', 0):.4f}
- Volume Ratio: {eth_data.get('volume_ratio', 1.0):.2f}x average
- Funding Rate: {eth_data.get('funding', 0):.4f}%
- OI Change: {eth_data.get('oi_change_pct', 0):+.2f}%

SOL/USDT-P:
- Price: ${sol_data.get('price', 0):.2f}
- RSI: {sol_data.get('rsi', 50):.1f}
- MACD: {sol_data.get('macd', 0):.4f}
- Volume Ratio: {sol_data.get('volume_ratio', 1.0):.2f}x average
- Funding Rate: {sol_data.get('funding', 0):.4f}%
- OI Change: {sol_data.get('oi_change_pct', 0):+.2f}%

SCORING RULES (0-1 points each):
1. RSI: <35 or >65 with direction = 1.0, neutral (45-55) = 0.0
2. MACD: Clear crossover = 1.0, flat = 0.0
3. Volume: >2x = 1.0, <1.2x = 0.0
4. Price Action: Breakout/bounce = 1.0, mid-range = 0.0
5. OI + Price: Confluence = 1.0, no data = 0.5

THRESHOLD: Score >= 3.0 to trade

Respond EXACTLY in this format:
BEST_ASSET: [ETH/USDT-P | SOL/USDT-P | NONE]
DIRECTION: [LONG | SHORT | NONE]
SCORE: [X.X/5.0]
BREAKDOWN: [RSI=X.X, MACD=X.X, Vol=X.X, PA=X.X, OI=X.X]
REASON: [Brief explanation]
"""

    def _build_sizing_prompt(
        self,
        symbol: str,
        direction: str,
        score: float,
        available_margin: float
    ) -> str:
        """Build prompt for position sizing"""
        return f"""You are a position sizing agent. Determine the optimal position size.

TRADE SETUP:
- Symbol: {symbol}
- Direction: {direction}
- Signal Score: {score}/5.0

ACCOUNT:
- Available Margin: ${available_margin:.2f}
- Leverage Range: {self.MIN_LEVERAGE}x - {self.MAX_LEVERAGE}x
- Max Position: $500 absolute limit

SIZING RULES:
- Higher score = larger position
- Score 3.0-3.5: Use {self.MIN_LEVERAGE}x leverage
- Score 3.5-4.0: Use 7x leverage
- Score 4.0+: Use {self.MAX_LEVERAGE}x leverage
- Never exceed available margin × leverage

Respond EXACTLY in this format:
POSITION_SIZE_USD: [number]
LEVERAGE: [X.X]x
REASON: [Brief explanation]
"""

    def _parse_scoring_response(self, response: str) -> Tuple[Optional[str], Optional[str], float, Dict]:
        """
        Parse LLM scoring response

        Returns: (symbol, direction, score, breakdown)
        """
        symbol = None
        direction = None
        score = 0.0
        breakdown = {}

        try:
            lines = response.strip().split('\n')
            for line in lines:
                line = line.strip()
                if line.startswith('BEST_ASSET:'):
                    asset = line.split(':')[1].strip()
                    if asset in self.ALLOWED_SYMBOLS:
                        symbol = asset
                    elif asset.upper() == 'NONE':
                        symbol = None
                elif line.startswith('DIRECTION:'):
                    dir_str = line.split(':')[1].strip().upper()
                    if dir_str in ['LONG', 'SHORT']:
                        direction = dir_str
                elif line.startswith('SCORE:'):
                    score_str = line.split(':')[1].strip()
                    score = float(score_str.split('/')[0])
                elif line.startswith('BREAKDOWN:'):
                    breakdown_str = line.split(':')[1].strip()
                    # Remove brackets if present (LLM often returns [RSI=0.8, ...])
                    breakdown_str = breakdown_str.strip('[]')
                    # Parse breakdown like "RSI=0.8, MACD=0.7, ..."
                    for part in breakdown_str.split(','):
                        if '=' in part:
                            key, val = part.strip().split('=')
                            # Strip any trailing brackets from value
                            val = val.strip().rstrip(']')
                            try:
                                breakdown[key.strip()] = float(val)
                            except ValueError:
                                pass  # Skip unparseable values

        except Exception as e:
            logger.error(f"Error parsing scoring response: {e}")

        return symbol, direction, score, breakdown

    def _parse_sizing_response(self, response: str) -> Tuple[float, float]:
        """
        Parse LLM sizing response

        Returns: (position_size_usd, leverage)
        """
        size = 0.0
        leverage = self.MIN_LEVERAGE

        try:
            lines = response.strip().split('\n')
            for line in lines:
                line = line.strip()
                if line.startswith('POSITION_SIZE_USD:'):
                    size_str = line.split(':')[1].strip()
                    size = float(size_str.replace('$', '').replace(',', ''))
                elif line.startswith('LEVERAGE:'):
                    lev_str = line.split(':')[1].strip()
                    leverage = float(lev_str.replace('x', ''))

        except Exception as e:
            logger.error(f"Error parsing sizing response: {e}")

        # Clamp leverage to range
        leverage = max(self.MIN_LEVERAGE, min(self.MAX_LEVERAGE, leverage))

        return size, leverage

    async def run_cycle(self):
        """Single decision cycle"""
        self.cycles_run += 1
        logger.info("")
        logger.info("=" * 70)
        logger.info(f"LLM SUPERVISOR CYCLE #{self.cycles_run} - {datetime.now().strftime('%H:%M:%S')}")
        logger.info("=" * 70)

        # 1. Check existing position
        if self.open_position:
            logger.info(f"Active position: {self.open_position}")
            await self._check_exit_conditions()
            return

        # 2. Check available margin
        available = await self.get_available_margin()
        logger.info(f"Available margin: ${available:.2f} (after ${self.MARGIN_BUFFER} buffer)")

        if available < 10:
            logger.info("Insufficient margin - skipping cycle")
            return

        # 3. Fetch market data for ETH and SOL
        eth_data = await self.get_market_data("ETH/USDT-P")
        sol_data = await self.get_market_data("SOL/USDT-P")

        if not eth_data or not sol_data:
            logger.error("Failed to fetch market data")
            return

        logger.info(f"ETH: ${eth_data['price']:.2f}")
        logger.info(f"SOL: ${sol_data['price']:.2f}")

        # 4. Query LLM for scoring
        scoring_prompt = self._build_scoring_prompt(eth_data, sol_data)
        scoring_result = self.llm_client.query(scoring_prompt, max_tokens=500)

        if not scoring_result or not scoring_result.get('content'):
            logger.error("LLM scoring query failed")
            return

        logger.info(f"LLM Response:\n{scoring_result['content']}")

        # 5. Parse scoring response
        symbol, direction, score, breakdown = self._parse_scoring_response(scoring_result['content'])

        logger.info(f"Parsed: Symbol={symbol}, Direction={direction}, Score={score:.1f}")
        logger.info(f"Breakdown: {breakdown}")

        # 6. Check if score meets threshold
        if score < self.SCORE_THRESHOLD:
            logger.info(f"NO_TRADE: Score {score:.1f} < {self.SCORE_THRESHOLD} threshold")
            return

        if not symbol or not direction:
            logger.info("NO_TRADE: No valid symbol/direction from LLM")
            return

        # 7. Query LLM for position sizing
        sizing_prompt = self._build_sizing_prompt(symbol, direction, score, available)
        sizing_result = self.llm_client.query(sizing_prompt, max_tokens=200)

        if not sizing_result or not sizing_result.get('content'):
            logger.error("LLM sizing query failed")
            return

        logger.info(f"Sizing Response:\n{sizing_result['content']}")

        position_size, leverage = self._parse_sizing_response(sizing_result['content'])
        logger.info(f"Position: ${position_size:.2f} @ {leverage:.1f}x leverage")

        # 8. Validate position size
        max_position = available * leverage
        if position_size > max_position:
            position_size = max_position
            logger.info(f"Capped position to ${position_size:.2f}")

        if position_size > 500:
            position_size = 500
            logger.info(f"Capped position to $500 max")

        if position_size < 10:
            logger.info("Position too small - skipping")
            return

        # 9. Execute trade
        await self._execute_trade(symbol, direction, position_size)

    async def _execute_trade(self, symbol: str, direction: str, size_usd: float):
        """Execute a trade"""
        # Safety check
        if symbol not in self.ALLOWED_SYMBOLS:
            logger.error(f"BLOCKED: {symbol} not in allowed list")
            return

        if symbol in self.FORBIDDEN_SYMBOLS:
            logger.error(f"BLOCKED: {symbol} reserved for Grid MM")
            return

        # Get current price for quantity calculation
        price = await self.sdk.get_price(symbol)
        if not price:
            logger.error("Cannot get price for trade")
            return

        # Calculate quantity
        # For perpetuals, quantity is in base currency (ETH or SOL)
        quantity = size_usd / price

        is_buy = direction == "LONG"
        action = "BUY" if is_buy else "SELL"

        logger.info(f"{'DRY RUN: ' if self.dry_run else ''}Executing {action} {quantity:.6f} {symbol} (${size_usd:.2f})")

        if self.dry_run:
            # Simulate trade
            self.open_position = {
                'symbol': symbol,
                'direction': 'Long' if is_buy else 'Short',
                'quantity': quantity,
                'entry_price': price
            }
            self.entry_price = price
            self.entry_time = datetime.now()
            self.trades_made += 1
            logger.info(f"DRY RUN: Position opened @ ${price:.2f}")
        else:
            # Live trade
            result = await self.sdk.create_market_order(symbol, is_buy, quantity)
            if result and not result.get('error'):
                self.open_position = {
                    'symbol': symbol,
                    'direction': 'Long' if is_buy else 'Short',
                    'quantity': quantity,
                    'entry_price': price
                }
                self.entry_price = price
                self.entry_time = datetime.now()
                self.trades_made += 1
                logger.info(f"Position opened @ ${price:.2f}")
            else:
                logger.error(f"Trade execution failed: {result}")

    async def _check_exit_conditions(self):
        """Check if position should be closed"""
        if not self.open_position:
            return

        symbol = self.open_position['symbol']
        direction = self.open_position['direction']
        entry = self.entry_price

        # Get current price
        current = await self.sdk.get_price(symbol)
        if not current:
            return

        # Calculate P&L
        if direction == 'Long':
            pnl_pct = ((current - entry) / entry) * 100
        else:
            pnl_pct = ((entry - current) / entry) * 100

        logger.info(f"Position: {direction} {symbol} @ ${entry:.2f} → ${current:.2f} ({pnl_pct:+.2f}%)")

        # Check exit conditions
        should_close = False
        reason = ""

        if pnl_pct >= self.TP_PCT:
            should_close = True
            reason = f"TP HIT: +{pnl_pct:.2f}%"
        elif pnl_pct <= -self.SL_PCT:
            should_close = True
            reason = f"SL HIT: {pnl_pct:.2f}%"
        elif self.entry_time and (datetime.now() - self.entry_time).total_seconds() > self.MAX_HOLD_HOURS * 3600:
            should_close = True
            reason = f"MAX HOLD: {self.MAX_HOLD_HOURS}h exceeded"

        if should_close:
            await self._close_position(reason, pnl_pct)

    async def _close_position(self, reason: str, pnl_pct: float):
        """Close the current position"""
        if not self.open_position:
            return

        symbol = self.open_position['symbol']
        direction = self.open_position['direction']
        quantity = self.open_position['quantity']

        is_buy = direction == 'Short'  # Close opposite direction
        action = "BUY" if is_buy else "SELL"

        logger.info(f"{'DRY RUN: ' if self.dry_run else ''}Closing position: {action} {quantity:.6f} {symbol}")
        logger.info(f"Reason: {reason}")

        if self.dry_run:
            # Simulate close
            logger.info(f"DRY RUN: Position closed, P&L: {pnl_pct:+.2f}%")
        else:
            # Live close
            result = await self.sdk.create_market_order(symbol, is_buy, abs(quantity))
            if result and not result.get('error'):
                logger.info(f"Position closed, P&L: {pnl_pct:+.2f}%")
            else:
                logger.error(f"Close failed: {result}")

        self.total_pnl += pnl_pct
        self.open_position = None
        self.entry_price = None
        self.entry_time = None

    async def fast_exit_monitor(self):
        """Fast exit monitoring loop - checks TP/SL every 30 seconds"""
        self.fast_exit_running = True
        logger.info(f"Fast exit monitor started (every {self.FAST_EXIT_INTERVAL}s)")

        check_count = 0
        while self.fast_exit_running:
            try:
                check_count += 1

                if self.open_position:
                    symbol = self.open_position['symbol']
                    direction = self.open_position['direction']
                    entry = self.entry_price

                    current = await self.sdk.get_price(symbol)
                    if current:
                        if direction == 'Long':
                            pnl_pct = ((current - entry) / entry) * 100
                        else:
                            pnl_pct = ((entry - current) / entry) * 100

                        # Log every 10th check (5 minutes)
                        if check_count % 10 == 0:
                            logger.info(f"[FAST-EXIT] Check #{check_count}: {symbol} {pnl_pct:+.2f}%")

                        # Check TP/SL
                        if pnl_pct >= self.TP_PCT:
                            logger.info(f"[FAST-EXIT] TP triggered!")
                            await self._close_position(f"TP HIT: +{pnl_pct:.2f}%", pnl_pct)
                        elif pnl_pct <= -self.SL_PCT:
                            logger.info(f"[FAST-EXIT] SL triggered!")
                            await self._close_position(f"SL HIT: {pnl_pct:.2f}%", pnl_pct)

                await asyncio.sleep(self.FAST_EXIT_INTERVAL)

            except Exception as e:
                logger.error(f"[FAST-EXIT] Error: {e}")
                await asyncio.sleep(self.FAST_EXIT_INTERVAL)

    async def run(self):
        """Main run loop"""
        logger.info("Starting LLM Supervisor...")

        # Start fast exit monitor as background task
        fast_exit_task = asyncio.create_task(self.fast_exit_monitor())

        try:
            while True:
                try:
                    await self.run_cycle()
                    logger.info(f"Next cycle in {self.CHECK_INTERVAL}s ({self.CHECK_INTERVAL // 60} min)")
                    await asyncio.sleep(self.CHECK_INTERVAL)
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    logger.error(f"Cycle error: {e}")
                    await asyncio.sleep(60)  # Backoff on error
        finally:
            self.fast_exit_running = False
            fast_exit_task.cancel()
            self._log_stats()

    def _log_stats(self):
        """Log final statistics"""
        runtime = (datetime.now() - self.start_time).total_seconds() / 3600 if self.start_time else 0
        logger.info("")
        logger.info("=" * 70)
        logger.info("LLM SUPERVISOR FINAL STATS")
        logger.info("=" * 70)
        logger.info(f"Runtime: {runtime:.2f} hours")
        logger.info(f"Cycles: {self.cycles_run}")
        logger.info(f"Trades: {self.trades_made}")
        logger.info(f"Total P&L: {self.total_pnl:+.2f}%")
        if self.llm_client:
            logger.info(f"LLM Spend: ${self.llm_client.get_daily_spend():.4f}")
        logger.info("=" * 70)


async def main():
    parser = argparse.ArgumentParser(description='LLM Directional Supervisor for Hibachi')
    parser.add_argument('--dry-run', action='store_true', default=True,
                        help='Run in simulation mode (default: True)')
    parser.add_argument('--live', action='store_true',
                        help='Run in live trading mode')
    args = parser.parse_args()

    dry_run = not args.live

    supervisor = HibachiLLMSupervisor(dry_run=dry_run)
    await supervisor.initialize()
    await supervisor.run()


if __name__ == "__main__":
    asyncio.run(main())
