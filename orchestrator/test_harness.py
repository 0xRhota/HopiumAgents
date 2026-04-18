"""
Test Harness for Swing Orchestrator

Validates all components work correctly before and during the 24-hour test.
"""

import asyncio
import sys
import os
from datetime import datetime, timezone
from typing import Dict, List, Tuple

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import config
from orchestrator import logger as log
from orchestrator.funding_monitor import FundingMonitor, get_monitor
from orchestrator.technical_engine import TechnicalEngine, get_engine
from orchestrator.position_manager import PositionManager, get_manager
from orchestrator.llm_decision_engine import LLMDecisionEngine, get_engine as get_llm_engine
from orchestrator.swing_orchestrator import SwingOrchestrator


class TestHarness:
    """
    Test harness for validating the swing orchestrator.

    Pre-flight checks:
    1. Can fetch funding rates from Binance
    2. Can calculate technical indicators
    3. Can connect to each exchange
    4. Can query balances
    5. Can query positions
    6. Logging is working

    Runtime monitoring:
    - Track errors
    - Generate hourly reports
    - Validate data accuracy
    """

    def __init__(self):
        self.results: Dict[str, Tuple[bool, str]] = {}
        self.orchestrator: SwingOrchestrator = None
        self.errors: List[str] = []

    async def run_preflight_checks(self) -> bool:
        """Run all pre-flight checks."""
        log.log_info("=" * 60)
        log.log_info("RUNNING PRE-FLIGHT CHECKS")
        log.log_info("=" * 60)

        checks = [
            ("Funding Rates", self._check_funding_rates),
            ("Technical Engine", self._check_technical_engine),
            ("LLM Decision Engine", self._check_llm_engine),
            ("Paradex Connection", self._check_paradex),
            ("Hibachi Connection", self._check_hibachi),
            ("Nado Connection", self._check_nado),
            ("Extended Connection", self._check_extended),
            ("Logging System", self._check_logging),
            ("Position Manager", self._check_position_manager),
        ]

        all_passed = True

        for name, check_func in checks:
            try:
                passed, message = await check_func()
                self.results[name] = (passed, message)

                status = "PASS" if passed else "FAIL"
                log.log_info(f"  [{status}] {name}: {message}")

                if not passed:
                    all_passed = False
                    self.errors.append(f"{name}: {message}")

            except Exception as e:
                self.results[name] = (False, str(e))
                log.log_error(e, f"preflight check: {name}")
                all_passed = False
                self.errors.append(f"{name}: {str(e)}")

        log.log_info("=" * 60)
        if all_passed:
            log.log_info("ALL PRE-FLIGHT CHECKS PASSED")
        else:
            log.log_info(f"PRE-FLIGHT CHECKS FAILED ({len(self.errors)} errors)")
            for err in self.errors:
                log.log_info(f"  - {err}")
        log.log_info("=" * 60)

        return all_passed

    async def _check_funding_rates(self) -> Tuple[bool, str]:
        """Check that we can fetch funding rates."""
        monitor = get_monitor()
        rates = await monitor.get_all_funding_rates()

        if not rates:
            return False, "No funding rates returned"

        if "binance" not in rates:
            return False, "Binance rates missing"

        btc_rate = rates.get("binance", {}).get("BTC")
        if btc_rate is None:
            return False, "BTC funding rate missing"

        return True, f"BTC funding: {btc_rate*100:.4f}%"

    async def _check_technical_engine(self) -> Tuple[bool, str]:
        """Check that we can calculate technical indicators."""
        engine = get_engine()
        analysis = await engine.analyze("BTC")

        if not analysis:
            return False, "No analysis returned"

        score = analysis.get("score", 0)
        indicators = analysis.get("indicators", {})

        if not indicators.get("rsi"):
            return False, "RSI missing from indicators"

        return True, f"BTC score: {score:.2f}, RSI: {indicators['rsi']:.1f}"

    async def _check_llm_engine(self) -> Tuple[bool, str]:
        """Check that LLM decision engine is configured."""
        import os
        engine = get_llm_engine()

        if not engine.api_key:
            return False, "OPEN_ROUTER API key not configured"

        return True, f"Model: {engine.model} (dynamic position sizing enabled)"

    async def _check_paradex(self) -> Tuple[bool, str]:
        """Check Paradex connection."""
        try:
            from orchestrator.agents.paradex_agent import ParadexAgent
            agent = ParadexAgent()

            if not await agent.initialize():
                return False, "Failed to initialize"

            balance = await agent.get_balance()
            if balance is None:
                return False, "Could not get balance"

            return True, f"Balance: ${balance:.2f}"
        except Exception as e:
            return False, str(e)

    async def _check_hibachi(self) -> Tuple[bool, str]:
        """Check Hibachi connection."""
        try:
            from orchestrator.agents.hibachi_agent import HibachiAgent
            agent = HibachiAgent()

            if not await agent.initialize():
                return False, "Failed to initialize"

            balance = await agent.get_balance()
            if balance is None:
                return False, "Could not get balance"

            return True, f"Balance: ${balance:.2f}"
        except Exception as e:
            return False, str(e)

    async def _check_nado(self) -> Tuple[bool, str]:
        """Check Nado connection."""
        try:
            from orchestrator.agents.nado_agent import NadoAgent
            agent = NadoAgent()

            if not await agent.initialize():
                return False, "Failed to initialize"

            balance = await agent.get_balance()
            if balance is None:
                return False, "Could not get balance"

            return True, f"Balance: ${balance:.2f}"
        except Exception as e:
            return False, str(e)

    async def _check_extended(self) -> Tuple[bool, str]:
        """Check Extended connection."""
        try:
            from orchestrator.agents.extended_agent import ExtendedAgent
            agent = ExtendedAgent()

            if not await agent.initialize():
                return False, "Failed to initialize"

            balance = await agent.get_balance()
            if balance is None:
                return False, "Could not get balance"

            return True, f"Balance: ${balance:.2f}"
        except Exception as e:
            return False, str(e)

    async def _check_logging(self) -> Tuple[bool, str]:
        """Check that logging is working."""
        from pathlib import Path

        log_dir = Path(config.LOG_DIR)
        log_dir.mkdir(exist_ok=True)

        # Test write
        test_file = log_dir / "test_check.txt"
        try:
            test_file.write_text("test")
            test_file.unlink()
            return True, f"Log directory: {log_dir}"
        except Exception as e:
            return False, f"Cannot write to log directory: {e}"

    async def _check_position_manager(self) -> Tuple[bool, str]:
        """Check position manager."""
        manager = get_manager()
        positions = manager.get_all_open_positions()
        return True, f"{len(positions)} open positions tracked"

    async def run_single_cycle(self) -> bool:
        """Run a single orchestrator cycle for testing."""
        log.log_info("=" * 60)
        log.log_info("RUNNING SINGLE TEST CYCLE")
        log.log_info("=" * 60)

        self.orchestrator = SwingOrchestrator()

        try:
            await self.orchestrator.initialize()
            await self.orchestrator.run_cycle()
            return True
        except Exception as e:
            log.log_error(e, "run_single_cycle")
            return False
        finally:
            await self.orchestrator.cleanup()

    async def run_24h_test(self) -> None:
        """Run the full 24-hour test."""
        log.log_info("=" * 60)
        log.log_info("STARTING 24-HOUR LIVE TEST")
        log.log_info(f"Start time: {datetime.now(timezone.utc).isoformat()}")
        log.log_info(f"Expected end: {datetime.now(timezone.utc).isoformat()} + 24h")
        log.log_info(f"Cycle interval: {config.CYCLE_INTERVAL_SECONDS}s")
        log.log_info(f"Expected cycles: {24 * 3600 // config.CYCLE_INTERVAL_SECONDS}")
        log.log_info("")
        log.log_info("DECISION ENGINE: LLM-DRIVEN (Qwen)")
        log.log_info("POSITION SIZING: DYNAMIC (LLM chooses based on conviction)")
        log.log_info("USER REQUIREMENT: 'Let it choose whatever size based on conviction'")
        log.log_info("=" * 60)

        self.orchestrator = SwingOrchestrator()

        try:
            await self.orchestrator.initialize()
            await self.orchestrator.run()
        except KeyboardInterrupt:
            log.log_info("Test interrupted by user")
        except Exception as e:
            log.log_error(e, "run_24h_test")
        finally:
            await self.orchestrator.cleanup()
            self._generate_final_report()

    def _generate_final_report(self) -> None:
        """Generate final test report."""
        log.log_info("=" * 60)
        log.log_info("24-HOUR TEST FINAL REPORT")
        log.log_info("=" * 60)

        if self.orchestrator:
            log.log_info(f"Total cycles run: {self.orchestrator.cycle_count}")

            if self.orchestrator.start_time:
                duration = datetime.now(timezone.utc) - self.orchestrator.start_time
                log.log_info(f"Duration: {duration}")

        log.log_info("=" * 60)
        log.log_info("Check logs for detailed results:")
        log.log_info(f"  - {config.LOG_DIR}/{config.LOG_FILE}")
        log.log_info(f"  - {config.LOG_DIR}/{config.DECISIONS_FILE}")
        log.log_info(f"  - {config.LOG_DIR}/{config.POSITIONS_FILE}")
        log.log_info(f"  - {config.LOG_DIR}/{config.FUNDING_FILE}")
        log.log_info(f"  - {config.LOG_DIR}/{config.PNL_FILE}")
        log.log_info("=" * 60)


async def main():
    """Main entry point for test harness."""
    import argparse

    parser = argparse.ArgumentParser(description="Swing Orchestrator Test Harness")
    parser.add_argument(
        "--mode",
        choices=["preflight", "single", "24h"],
        default="preflight",
        help="Test mode: preflight (checks only), single (one cycle), 24h (full test)"
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip pre-flight checks (not recommended)"
    )

    args = parser.parse_args()

    harness = TestHarness()

    # Always run preflight unless skipped
    if not args.skip_preflight:
        passed = await harness.run_preflight_checks()

        if not passed and args.mode != "preflight":
            log.log_info("Pre-flight checks failed. Fix issues before running tests.")
            log.log_info("Use --skip-preflight to run anyway (not recommended)")
            return

    if args.mode == "single":
        await harness.run_single_cycle()

    elif args.mode == "24h":
        await harness.run_24h_test()


if __name__ == "__main__":
    asyncio.run(main())
