"""
Paper Trading Test Runner

Runs 4 parallel paper trading tests for 24 hours:
1. GPT + Current Strategy (verbose LLM engine)
2. Qwen + Current Strategy (verbose LLM engine)
3. GPT + Simplified Strategy (GPT-style engine)
4. Qwen + Simplified Strategy (GPT-style engine)

Each test has its own:
- Paper balance tracking
- Position tracking
- Trade history
- Performance metrics
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
import logging

from . import config
from . import logger as log
from .funding_monitor import FundingMonitor, get_monitor as get_funding_monitor
from .technical_engine import TechnicalEngine, get_engine as get_technical_engine

# Import both engines
from .llm_decision_engine import LLMDecisionEngine
from .simplified_decision_engine import SimplifiedDecisionEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class TestConfig:
    """Configuration for a single test."""
    name: str
    engine_type: str  # "current" or "simplified"
    model: str  # "qwen" or "gpt"
    balance_per_exchange: float = 100.0


@dataclass
class TestState:
    """State for a single test."""
    config: TestConfig
    balances: Dict[str, float] = field(default_factory=dict)
    positions: Dict[str, Dict] = field(default_factory=dict)  # key -> position
    trades: List[Dict] = field(default_factory=list)
    cycle_count: int = 0
    start_time: Optional[datetime] = None

    # Metrics
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    peak_balance: float = 0.0
    max_drawdown: float = 0.0


@dataclass
class TestResult:
    """Final result for a test."""
    name: str
    engine_type: str
    model: str
    duration_hours: float
    cycles_completed: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    return_pct: float
    max_drawdown_pct: float
    initial_balance: float
    final_balance: float
    trades: List[Dict] = field(default_factory=list)


class PaperTestRunner:
    """
    Runs multiple paper trading tests in parallel.
    """

    # Test configurations
    # Using qwen-max for all tests (DeepSeek API key invalid)
    # Comparing: Current (verbose) vs Simplified (GPT-style) decision engines
    # Running 2 instances of each to get statistical significance
    TEST_CONFIGS = [
        TestConfig(name="Current_A", engine_type="current", model="qwen-max"),
        TestConfig(name="Current_B", engine_type="current", model="qwen-max"),
        TestConfig(name="Simplified_A", engine_type="simplified", model="qwen-max"),
        TestConfig(name="Simplified_B", engine_type="simplified", model="qwen-max"),
    ]

    # Available exchanges for testing (simplified - use only ones with good APIs)
    EXCHANGES = ["hibachi", "extended"]

    # Symbols to trade
    SYMBOLS = ["BTC", "ETH", "SOL"]

    def __init__(self, duration_hours: float = 24.0, cycle_minutes: float = 15.0):
        """
        Initialize test runner.

        Args:
            duration_hours: How long to run tests (default 24h)
            cycle_minutes: Minutes between cycles (default 15)
        """
        self.duration_hours = duration_hours
        self.cycle_seconds = cycle_minutes * 60
        self.running = False

        # Shared components (market data is same for all tests)
        self.funding_monitor: Optional[FundingMonitor] = None
        self.technical_engine: Optional[TechnicalEngine] = None

        # Test states
        self.test_states: Dict[str, TestState] = {}
        self.engines: Dict[str, Any] = {}

        # Results file
        self.results_file = f"logs/paper_test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    async def initialize(self) -> bool:
        """Initialize all components and test states."""
        logger.info("=" * 60)
        logger.info("PAPER TRADING TEST RUNNER")
        logger.info(f"Duration: {self.duration_hours} hours")
        logger.info(f"Cycle interval: {self.cycle_seconds / 60:.0f} minutes")
        logger.info(f"Tests: {len(self.TEST_CONFIGS)}")
        logger.info("=" * 60)

        # Initialize shared market data components
        self.funding_monitor = get_funding_monitor()
        self.technical_engine = get_technical_engine()

        # Initialize each test
        for test_config in self.TEST_CONFIGS:
            logger.info(f"Initializing test: {test_config.name}")

            # Create engine based on type
            # Models are already in correct format: deepseek-chat, qwen-max
            if test_config.engine_type == "simplified":
                engine = SimplifiedDecisionEngine(model=test_config.model)
            else:
                engine = LLMDecisionEngine(model=test_config.model)

            self.engines[test_config.name] = engine

            # Create test state with initial balances
            state = TestState(config=test_config)
            for exchange in self.EXCHANGES:
                state.balances[exchange] = test_config.balance_per_exchange
            state.start_time = datetime.now(timezone.utc)
            state.peak_balance = sum(state.balances.values())

            self.test_states[test_config.name] = state
            logger.info(f"  Engine: {test_config.engine_type}")
            logger.info(f"  Model: {test_config.model}")
            logger.info(f"  Initial balance: ${sum(state.balances.values()):.2f}")

        logger.info("=" * 60)
        return True

    async def run_cycle(self) -> None:
        """Run a single decision cycle for all tests."""
        cycle_start = time.time()

        # Fetch shared market data (same for all tests)
        funding_data = await self._fetch_funding()
        technical_data = await self._fetch_technicals()

        if not technical_data:
            logger.warning("No technical data available, skipping cycle")
            return

        # Run each test in parallel
        tasks = []
        for test_name, state in self.test_states.items():
            engine = self.engines[test_name]
            task = self._run_test_cycle(test_name, state, engine, funding_data, technical_data)
            tasks.append(task)

        await asyncio.gather(*tasks, return_exceptions=True)

        cycle_duration = time.time() - cycle_start
        logger.info(f"Cycle completed in {cycle_duration:.1f}s")

    async def _run_test_cycle(
        self,
        test_name: str,
        state: TestState,
        engine: Any,
        funding_data: Dict,
        technical_data: Dict
    ) -> None:
        """Run a single cycle for one test."""
        state.cycle_count += 1

        try:
            # Update positions with current prices
            await self._update_positions(state, technical_data)

            # Check exits
            await self._check_exits(state, technical_data)

            # Get positions in format engine expects
            positions = self._format_positions(state)

            # Get decision from engine
            decision = await engine.get_decision(
                balances=state.balances.copy(),
                positions=positions,
                funding_data=funding_data,
                technical_data=technical_data
            )

            logger.info(f"[{test_name}] Cycle {state.cycle_count}: {decision.get('decision', 'NO_TRADE')}")

            # Execute decision
            if decision.get("decision") == "TRADE":
                await self._execute_trade(state, decision, technical_data)

            # Update metrics
            self._update_metrics(state)

        except Exception as e:
            logger.error(f"[{test_name}] Cycle error: {e}")

    async def _fetch_funding(self) -> Dict[str, Dict[str, float]]:
        """Fetch funding rates."""
        try:
            return await self.funding_monitor.get_all_funding_rates()
        except Exception as e:
            logger.error(f"Funding fetch error: {e}")
            return {}

    async def _fetch_technicals(self) -> Dict[str, Dict]:
        """Fetch technical data for all symbols."""
        results = {}
        for symbol in self.SYMBOLS:
            try:
                analysis = await self.technical_engine.analyze(symbol)
                if analysis:
                    results[symbol] = analysis
            except Exception as e:
                logger.error(f"Technical fetch error for {symbol}: {e}")
        return results

    async def _update_positions(self, state: TestState, technical_data: Dict) -> None:
        """Update position prices and P&L."""
        for key, pos in state.positions.items():
            symbol = pos["symbol"]
            if symbol in technical_data:
                current_price = technical_data[symbol].get("indicators", {}).get("price", 0)
                if current_price:
                    pos["current_price"] = current_price

                    entry_price = pos["entry_price"]
                    if pos["side"] == "LONG":
                        pnl_pct = ((current_price - entry_price) / entry_price) * 100
                    else:
                        pnl_pct = ((entry_price - current_price) / entry_price) * 100

                    pos["unrealized_pnl"] = pos["size_usd"] * (pnl_pct / 100)
                    pos["unrealized_pnl_pct"] = pnl_pct

    async def _check_exits(self, state: TestState, technical_data: Dict) -> None:
        """Check and execute exits for positions."""
        to_close = []

        for key, pos in state.positions.items():
            pnl_pct = pos.get("unrealized_pnl_pct", 0)

            # Check TP/SL (3%/-2% for simplified, 5%/-2% for current)
            tp_pct = 3.0 if "Simplified" in state.config.name else 5.0
            sl_pct = -2.0

            exit_reason = None
            if pnl_pct >= tp_pct:
                exit_reason = f"TP hit ({pnl_pct:.1f}%)"
            elif pnl_pct <= sl_pct:
                exit_reason = f"SL hit ({pnl_pct:.1f}%)"

            # Time stop (24h for current, 12h for simplified)
            max_hours = 12 if "Simplified" in state.config.name else 24
            entry_time = datetime.fromisoformat(pos["entry_time"].replace("Z", "+00:00"))
            hours_held = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
            if hours_held >= max_hours:
                exit_reason = f"Time stop ({hours_held:.1f}h)"

            if exit_reason:
                to_close.append((key, pos, exit_reason))

        # Close positions
        for key, pos, reason in to_close:
            self._close_position(state, key, pos, reason)

    def _close_position(self, state: TestState, key: str, pos: Dict, reason: str) -> None:
        """Close a position and record the trade."""
        pnl = pos.get("unrealized_pnl", 0)
        pnl_pct = pos.get("unrealized_pnl_pct", 0)

        # Update balance
        exchange = key.split("_")[0]
        state.balances[exchange] = state.balances.get(exchange, 0) + pos["size_usd"] + pnl

        # Record trade
        trade = {
            "type": "CLOSE",
            "exchange": exchange,
            "symbol": pos["symbol"],
            "side": pos["side"],
            "size_usd": pos["size_usd"],
            "entry_price": pos["entry_price"],
            "exit_price": pos["current_price"],
            "pnl_usd": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        state.trades.append(trade)

        # Update win/loss counters
        state.total_trades += 1
        if pnl > 0:
            state.winning_trades += 1
        else:
            state.losing_trades += 1
        state.total_pnl += pnl

        # Remove position
        del state.positions[key]

        logger.info(f"[{state.config.name}] CLOSED {pos['side']} {pos['symbol']}: ${pnl:+.2f} ({pnl_pct:+.1f}%) - {reason}")

    async def _execute_trade(self, state: TestState, decision: Dict, technical_data: Dict) -> None:
        """Execute a trade decision."""
        symbol = decision.get("symbol")
        direction = decision.get("direction")
        exchange = decision.get("exchange")
        size_usd = decision.get("size_usd", 0)

        # Validate
        if not all([symbol, direction, exchange]):
            return

        # Check if already have position in this symbol
        position_key = f"{exchange}_{symbol}"
        if position_key in state.positions:
            logger.info(f"[{state.config.name}] Already have position in {symbol}")
            return

        # Check balance
        available = state.balances.get(exchange, 0)
        if size_usd > available:
            size_usd = available * 0.9  # Use 90% of available

        min_order = config.EXCHANGE_CONFIG.get(exchange, {}).get("min_order_usd", 10)
        if size_usd < min_order:
            logger.info(f"[{state.config.name}] Size ${size_usd:.2f} below minimum ${min_order}")
            return

        # Get price
        price = technical_data.get(symbol, {}).get("indicators", {}).get("price", 0)
        if not price:
            return

        # Create position
        state.positions[position_key] = {
            "symbol": symbol,
            "side": direction,
            "size": size_usd / price,
            "size_usd": size_usd,
            "entry_price": price,
            "current_price": price,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "unrealized_pnl": 0.0,
            "unrealized_pnl_pct": 0.0
        }

        # Deduct from balance
        state.balances[exchange] -= size_usd

        # Record trade
        trade = {
            "type": "OPEN",
            "exchange": exchange,
            "symbol": symbol,
            "side": direction,
            "size_usd": size_usd,
            "price": price,
            "conviction": decision.get("conviction", "MEDIUM"),
            "reasoning": decision.get("reasoning", ""),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        state.trades.append(trade)

        logger.info(f"[{state.config.name}] OPENED {direction} {symbol} @ ${price:,.2f} (${size_usd:.2f})")

    def _format_positions(self, state: TestState) -> Dict[str, List[Dict]]:
        """Format positions for engine consumption."""
        positions = {ex: [] for ex in self.EXCHANGES}
        for key, pos in state.positions.items():
            exchange = key.split("_")[0]
            positions[exchange].append(pos)
        return positions

    def _update_metrics(self, state: TestState) -> None:
        """Update performance metrics."""
        # Calculate total balance including unrealized
        total = sum(state.balances.values())
        unrealized = sum(p.get("unrealized_pnl", 0) for p in state.positions.values())
        equity = total + unrealized

        # Track peak and drawdown
        if equity > state.peak_balance:
            state.peak_balance = equity

        if state.peak_balance > 0:
            drawdown = (state.peak_balance - equity) / state.peak_balance * 100
            if drawdown > state.max_drawdown:
                state.max_drawdown = drawdown

    def get_results(self) -> List[TestResult]:
        """Get final results for all tests."""
        results = []

        for test_name, state in self.test_states.items():
            initial_balance = state.config.balance_per_exchange * len(self.EXCHANGES)
            final_balance = sum(state.balances.values())
            unrealized = sum(p.get("unrealized_pnl", 0) for p in state.positions.values())
            final_equity = final_balance + unrealized

            duration = 0
            if state.start_time:
                duration = (datetime.now(timezone.utc) - state.start_time).total_seconds() / 3600

            win_rate = 0
            if state.total_trades > 0:
                win_rate = state.winning_trades / state.total_trades * 100

            return_pct = ((final_equity - initial_balance) / initial_balance) * 100 if initial_balance > 0 else 0

            result = TestResult(
                name=test_name,
                engine_type=state.config.engine_type,
                model=state.config.model,
                duration_hours=duration,
                cycles_completed=state.cycle_count,
                total_trades=state.total_trades,
                winning_trades=state.winning_trades,
                losing_trades=state.losing_trades,
                win_rate=win_rate,
                total_pnl=state.total_pnl,
                return_pct=return_pct,
                max_drawdown_pct=state.max_drawdown,
                initial_balance=initial_balance,
                final_balance=final_equity,
                trades=state.trades
            )
            results.append(result)

        return results

    def print_summary(self) -> None:
        """Print summary of all test results."""
        results = self.get_results()

        print("\n" + "=" * 80)
        print("PAPER TRADING TEST RESULTS")
        print("=" * 80)

        # Sort by return
        results.sort(key=lambda x: x.return_pct, reverse=True)

        print(f"\n{'Test':<20} {'Engine':<12} {'Model':<8} {'Trades':<8} {'Win%':<8} {'P&L':<10} {'Return%':<10} {'MaxDD%':<8}")
        print("-" * 80)

        for r in results:
            print(f"{r.name:<20} {r.engine_type:<12} {r.model:<8} {r.total_trades:<8} {r.win_rate:<8.1f} ${r.total_pnl:<9.2f} {r.return_pct:<10.2f} {r.max_drawdown_pct:<8.2f}")

        print("\n" + "=" * 80)

        # Winner
        if results:
            winner = results[0]
            print(f"\nWINNER: {winner.name}")
            print(f"  Return: {winner.return_pct:+.2f}%")
            print(f"  Win Rate: {winner.win_rate:.1f}%")
            print(f"  Total P&L: ${winner.total_pnl:+.2f}")

    def save_results(self) -> None:
        """Save results to JSON file."""
        results = self.get_results()

        # Convert to dicts
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_hours": self.duration_hours,
            "results": [asdict(r) for r in results]
        }

        os.makedirs("logs", exist_ok=True)
        with open(self.results_file, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Results saved to {self.results_file}")

    async def run(self) -> None:
        """Run all tests for the specified duration."""
        self.running = True
        start_time = time.time()
        end_time = start_time + (self.duration_hours * 3600)

        logger.info(f"Starting {self.duration_hours}h paper trading test...")
        logger.info(f"Estimated end time: {datetime.now() + timedelta(hours=self.duration_hours)}")

        cycle = 0
        while self.running and time.time() < end_time:
            cycle += 1
            remaining = (end_time - time.time()) / 3600
            logger.info(f"\n--- Cycle {cycle} | {remaining:.1f}h remaining ---")

            await self.run_cycle()

            # Print interim summary every 4 cycles (1 hour)
            if cycle % 4 == 0:
                self.print_summary()
                self.save_results()

            # Wait for next cycle
            sleep_time = min(self.cycle_seconds, end_time - time.time())
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        # Final results
        logger.info("\n" + "=" * 80)
        logger.info("TEST COMPLETE")
        logger.info("=" * 80)

        self.print_summary()
        self.save_results()

    def stop(self) -> None:
        """Stop the test runner."""
        self.running = False

    async def cleanup(self) -> None:
        """Cleanup resources."""
        if self.funding_monitor:
            await self.funding_monitor.close()
        if self.technical_engine:
            await self.technical_engine.close()


async def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Paper Trading Test Runner")
    parser.add_argument("--hours", type=float, default=24.0, help="Test duration in hours")
    parser.add_argument("--interval", type=float, default=15.0, help="Cycle interval in minutes")
    args = parser.parse_args()

    runner = PaperTestRunner(duration_hours=args.hours, cycle_minutes=args.interval)

    try:
        await runner.initialize()
        await runner.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        runner.stop()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
