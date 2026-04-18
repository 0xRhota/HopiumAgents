"""
Swing Trading Orchestrator

Main orchestrator that runs every 15 minutes to:
1. Fetch funding rates
2. Calculate technical scores
3. Make trading decisions
4. Execute via exchange agents
5. Monitor positions
6. Log everything accurately
"""

import asyncio
import time
from typing import Dict, List, Optional
from datetime import datetime, timezone

from . import config
from . import logger as log
from .funding_monitor import FundingMonitor, get_monitor as get_funding_monitor
from .technical_engine import TechnicalEngine, get_engine as get_technical_engine
from .position_manager import PositionManager, get_manager as get_position_manager
from .llm_decision_engine import LLMDecisionEngine, get_engine as get_llm_engine
from .agents.base_agent import BaseAgent
from .agents.paradex_agent import ParadexAgent
from .agents.hibachi_agent import HibachiAgent
from .agents.nado_agent import NadoAgent
from .agents.extended_agent import ExtendedAgent


class SwingOrchestrator:
    """
    Main orchestrator for swing trading.

    Runs 15-minute cycles:
    1. Fetch funding rates from all sources
    2. Calculate technical scores for all assets
    3. Make swing/scalp/no_trade decisions
    4. Route to appropriate exchange agent
    5. Monitor existing positions for exits
    6. Log all data accurately

    SHORT BIAS: 49.4% WR vs 41.8% LONG (7.6% edge)

    PAPER TRADING MODE:
    When config.PAPER_TRADE is True:
    - Uses simulated $100 balance per exchange
    - Simulates order execution (no real orders)
    - Tracks simulated positions with real price data
    - All market data (prices, funding, indicators) is REAL
    """

    def __init__(self):
        self.cycle_count = 0
        self.running = False
        self.start_time: Optional[datetime] = None

        # Paper trading state
        self.paper_mode = config.PAPER_TRADE
        self.paper_balances: Dict[str, float] = {}
        self.paper_positions: Dict[str, Dict] = {}  # exchange_symbol -> position
        self.paper_trades: List[Dict] = []

        # Components
        self.funding_monitor: Optional[FundingMonitor] = None
        self.technical_engine: Optional[TechnicalEngine] = None
        self.position_manager: Optional[PositionManager] = None
        self.llm_engine: Optional[LLMDecisionEngine] = None

        # Agents
        self.agents: Dict[str, BaseAgent] = {}

        # Tradeable symbols per exchange - will be populated dynamically from agents
        self.symbols_by_exchange = {}

    async def initialize(self) -> bool:
        """Initialize all components."""
        log.log_info("Initializing Swing Orchestrator...")

        if self.paper_mode:
            log.log_info("=" * 40)
            log.log_info("PAPER TRADING MODE ENABLED")
            log.log_info(f"Simulated balance: ${config.PAPER_BALANCE_PER_EXCHANGE:.2f} per exchange")
            log.log_info("All market data is REAL, only balances/trades are simulated")
            log.log_info("=" * 40)

            # Initialize paper balances
            for exchange in self.symbols_by_exchange.keys():
                self.paper_balances[exchange] = config.PAPER_BALANCE_PER_EXCHANGE

        # Initialize monitors
        self.funding_monitor = get_funding_monitor()
        self.technical_engine = get_technical_engine()
        self.position_manager = get_position_manager()
        self.llm_engine = get_llm_engine()

        log.log_info("LLM Decision Engine initialized (dynamic position sizing enabled)")

        # Initialize agents (for price data even in paper mode)
        success = True

        # Paradex
        self.agents["paradex"] = ParadexAgent()
        if not await self.agents["paradex"].initialize():
            log.log_warning("Paradex agent failed to initialize")
            success = False

        # Hibachi
        self.agents["hibachi"] = HibachiAgent()
        if not await self.agents["hibachi"].initialize():
            log.log_warning("Hibachi agent failed to initialize")
            success = False

        # Nado
        self.agents["nado"] = NadoAgent()
        if not await self.agents["nado"].initialize():
            log.log_warning("Nado agent failed to initialize")
            success = False

        # Extended
        self.agents["extended"] = ExtendedAgent()
        if not await self.agents["extended"].initialize():
            log.log_warning("Extended agent failed to initialize")
            success = False

        if success:
            log.log_info("All agents initialized successfully")
        else:
            log.log_warning("Some agents failed to initialize - will continue with available ones")

        # Populate symbols_by_exchange from agents' dynamic market lists
        for name, agent in self.agents.items():
            if hasattr(agent, 'get_available_symbols') and agent.initialized:
                symbols = agent.get_available_symbols()
                self.symbols_by_exchange[name] = symbols
                log.log_info(f"{name} markets: {symbols}")
            elif agent.initialized:
                # Fallback for agents without dynamic markets
                self.symbols_by_exchange[name] = []

        total_markets = sum(len(s) for s in self.symbols_by_exchange.values())
        log.log_info(f"Total available markets across all exchanges: {total_markets}")

        return True  # Continue even with partial initialization

    async def run_cycle(self) -> None:
        """Run a single decision cycle."""
        self.cycle_count += 1
        cycle_start = time.time()

        log.log_cycle_start(self.cycle_count)

        try:
            # Step 1: Fetch funding rates
            funding_data = await self._fetch_funding_rates()

            # Step 2: Get balances from all exchanges
            balances = await self._fetch_balances()
            log.log_balances(balances)

            # Step 3: Get positions from all exchanges
            positions = await self._fetch_positions()
            log.log_positions(positions)

            # Step 4: Sync position manager with exchange state
            for exchange, pos_list in positions.items():
                self.position_manager.sync_with_exchange(exchange, pos_list)

            # Step 5: Check exit rules for existing positions
            await self._check_exits(positions)

            # Step 6: Calculate technical scores for all symbols
            technical_data = await self._calculate_technical_scores()

            # Step 7: Make trading decisions (LLM-driven, dynamic sizing)
            decisions = await self._make_decisions(
                funding_data, technical_data, balances, positions
            )

            # Step 8: Execute trades
            for decision in decisions:
                if decision["action"] != "NO_TRADE":
                    await self._execute_decision(decision)
                else:
                    log.log_decision(decision)

            # Step 9: Log P&L
            pnl_data = await self._fetch_pnl()
            log.log_pnl(pnl_data)

        except Exception as e:
            log.log_error(e, "run_cycle")

        cycle_duration = time.time() - cycle_start
        log.log_cycle_end(self.cycle_count, cycle_duration)

    async def _fetch_funding_rates(self) -> Dict[str, Dict[str, float]]:
        """Fetch funding rates from all sources."""
        try:
            funding_data = await self.funding_monitor.get_all_funding_rates()
            log.log_funding_rates(funding_data)
            return funding_data
        except Exception as e:
            log.log_error(e, "_fetch_funding_rates")
            return {}

    async def _fetch_balances(self) -> Dict[str, float]:
        """Fetch balances from all exchanges (or use paper balances)."""
        if self.paper_mode:
            # Use paper balances, adjusted for open positions
            balances = {}
            for exchange in self.symbols_by_exchange.keys():
                base_balance = self.paper_balances.get(exchange, config.PAPER_BALANCE_PER_EXCHANGE)
                # Subtract capital in open positions
                position_value = sum(
                    pos.get("size_usd", 0)
                    for key, pos in self.paper_positions.items()
                    if key.startswith(exchange)
                )
                balances[exchange] = base_balance - position_value
            return balances

        # Real mode - fetch from exchanges
        balances = {}
        for name, agent in self.agents.items():
            try:
                balance = await agent.get_balance()
                balances[name] = balance if balance is not None else 0.0
            except Exception as e:
                log.log_error(e, f"_fetch_balances({name})")
                balances[name] = 0.0

        return balances

    async def _fetch_positions(self) -> Dict[str, List[Dict]]:
        """Fetch positions from all exchanges (or use paper positions)."""
        if self.paper_mode:
            # Return paper positions, updating current prices
            positions = {exchange: [] for exchange in self.symbols_by_exchange.keys()}

            for key, pos in list(self.paper_positions.items()):
                exchange = key.split("_")[0]
                symbol = pos["symbol"]

                # Get current price from technical engine cache or agents
                current_price = None
                if self.technical_engine and symbol in self.technical_engine._cache:
                    current_price = self.technical_engine._cache[symbol].get("price")

                if current_price is None:
                    # Try to get from agent
                    agent = self.agents.get(exchange)
                    if agent:
                        try:
                            current_price = await agent.get_current_price(symbol)
                        except:
                            pass

                if current_price is None:
                    current_price = pos.get("current_price", pos["entry_price"])

                # Update position with current price and P&L
                entry_price = pos["entry_price"]
                if pos["side"] == "LONG":
                    pnl_pct = ((current_price - entry_price) / entry_price) * 100
                else:
                    pnl_pct = ((entry_price - current_price) / entry_price) * 100

                pos["current_price"] = current_price
                pos["unrealized_pnl"] = pos["size_usd"] * (pnl_pct / 100)
                pos["unrealized_pnl_pct"] = pnl_pct

                positions[exchange].append(pos)

            return positions

        # Real mode - fetch from exchanges
        positions = {}
        for name, agent in self.agents.items():
            try:
                pos_list = await agent.get_positions()
                positions[name] = pos_list
            except Exception as e:
                log.log_error(e, f"_fetch_positions({name})")
                positions[name] = []

        return positions

    async def _fetch_pnl(self) -> Dict[str, Dict]:
        """Fetch P&L from all exchanges (or calculate paper P&L)."""
        if self.paper_mode:
            # Calculate paper P&L
            pnl_data = {}

            for exchange in self.symbols_by_exchange.keys():
                # Calculate unrealized from open positions
                unrealized = sum(
                    pos.get("unrealized_pnl", 0)
                    for key, pos in self.paper_positions.items()
                    if key.startswith(exchange)
                )

                # Calculate realized from closed trades
                realized = sum(
                    trade.get("pnl_usd", 0)
                    for trade in self.paper_trades
                    if trade.get("type") == "CLOSE" and trade.get("exchange") == exchange
                )

                pnl_data[exchange] = {
                    "realized": realized,
                    "unrealized": unrealized,
                    "fees": 0  # Paper trades have no fees
                }

            # Log paper trading summary
            total_trades = len(self.paper_trades)
            open_positions = len(self.paper_positions)
            closed_trades = sum(1 for t in self.paper_trades if t.get("type") == "CLOSE")
            total_realized = sum(t.get("pnl_usd", 0) for t in self.paper_trades if t.get("type") == "CLOSE")

            log.log_info(f"PAPER TRADING SUMMARY:")
            log.log_info(f"  Total trades: {total_trades}")
            log.log_info(f"  Open positions: {open_positions}")
            log.log_info(f"  Closed trades: {closed_trades}")
            log.log_info(f"  Realized P&L: ${total_realized:+.2f}")

            return pnl_data

        # Real mode - fetch from exchanges
        pnl_data = {}
        for name, agent in self.agents.items():
            try:
                pnl = await agent.get_pnl()
                pnl_data[name] = pnl
            except Exception as e:
                log.log_error(e, f"_fetch_pnl({name})")
                pnl_data[name] = {"realized": 0, "unrealized": 0, "fees": 0}

        return pnl_data

    async def _calculate_technical_scores(self) -> Dict[str, Dict]:
        """Calculate technical scores for all symbols."""
        results = {}

        # Analyze all unique symbols
        all_symbols = set()
        for symbols in self.symbols_by_exchange.values():
            all_symbols.update(symbols)

        # Remove blocked symbols
        all_symbols -= set(config.BLOCKED_ASSETS)

        for symbol in all_symbols:
            try:
                analysis = await self.technical_engine.analyze(symbol)
                if analysis:
                    results[symbol] = analysis
                    log.log_technical_analysis(
                        symbol,
                        analysis["indicators"],
                        analysis["score"]
                    )
            except Exception as e:
                log.log_error(e, f"_calculate_technical_scores({symbol})")

        return results

    async def _check_exits(self, exchange_positions: Dict[str, List[Dict]]) -> None:
        """Check exit rules for all open positions (real or paper)."""

        if self.paper_mode:
            # Check paper positions
            await self._check_paper_exits(exchange_positions)
        else:
            # Check real positions
            await self._check_real_exits(exchange_positions)

    async def _check_paper_exits(self, exchange_positions: Dict[str, List[Dict]]) -> None:
        """Check exit rules for paper positions."""
        positions_to_close = []

        for position in self.position_manager.get_all_open_positions():
            try:
                # Get current price from exchange_positions (already updated with real prices)
                current_price = None
                for ep in exchange_positions.get(position.exchange, []):
                    if position.symbol in ep.get("symbol", ""):
                        current_price = ep.get("current_price")
                        break

                if current_price is None:
                    # Try technical engine cache
                    if self.technical_engine and position.symbol in self.technical_engine._cache:
                        current_price = self.technical_engine._cache[position.symbol].get("price")

                if current_price is None:
                    log.log_warning(f"Could not get price for {position.symbol} to check exits")
                    continue

                # Check exit rules
                exit_reason = self.position_manager.check_exit_rules(position, current_price)

                if exit_reason:
                    positions_to_close.append((position, current_price, exit_reason))

            except Exception as e:
                log.log_error(e, f"_check_paper_exits({position.id})")

        # Close positions
        for position, price, reason in positions_to_close:
            log.log_info(f"PAPER EXIT triggered for {position.id}: {reason}")

            # Calculate P&L
            pnl = self.position_manager.calculate_pnl(position, price)

            # Update paper balance
            self.paper_balances[position.exchange] = self.paper_balances.get(
                position.exchange, config.PAPER_BALANCE_PER_EXCHANGE
            ) + position.size_usd + pnl["pnl_usd"]

            # Remove from paper positions
            position_key = f"{position.exchange}_{position.symbol}"
            if position_key in self.paper_positions:
                del self.paper_positions[position_key]

            # Record close trade
            trade = {
                "type": "CLOSE",
                "exchange": position.exchange,
                "symbol": position.symbol,
                "direction": position.direction,
                "size": position.size,
                "size_usd": position.size_usd,
                "entry_price": position.entry_price,
                "exit_price": price,
                "pnl_usd": pnl["pnl_usd"],
                "pnl_pct": pnl["pnl_pct"],
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            self.paper_trades.append(trade)

            # Close in position manager
            self.position_manager.close_position(position, price, reason)

            log.log_info(f"PAPER TRADE CLOSED: {position.direction} {position.symbol} @ ${price:,.2f}")
            log.log_info(f"  P&L: ${pnl['pnl_usd']:+.2f} ({pnl['pnl_pct']:+.1f}%)")

    async def _check_real_exits(self, exchange_positions: Dict[str, List[Dict]]) -> None:
        """Check exit rules for real positions."""
        for position in self.position_manager.get_all_open_positions():
            try:
                # Get current price from the relevant exchange
                agent = self.agents.get(position.exchange)
                if not agent:
                    continue

                # Find matching exchange position for current price
                current_price = None
                for ep in exchange_positions.get(position.exchange, []):
                    if position.symbol in ep.get("symbol", ""):
                        current_price = ep.get("current_price")
                        break

                if current_price is None:
                    # Try to get price directly
                    current_price = await agent.get_current_price(position.symbol)

                if current_price is None:
                    log.log_warning(f"Could not get price for {position.symbol} to check exits")
                    continue

                # Check exit rules
                exit_reason = self.position_manager.check_exit_rules(position, current_price)

                if exit_reason:
                    log.log_info(f"Exit triggered for {position.id}: {exit_reason}")

                    # Close on exchange
                    result = await agent.close_position(position.symbol)

                    if result.get("success"):
                        self.position_manager.close_position(
                            position,
                            current_price,
                            exit_reason
                        )
                    else:
                        log.log_error(
                            Exception(result.get("error", "Unknown")),
                            f"Failed to close position {position.id}"
                        )

            except Exception as e:
                log.log_error(e, f"_check_real_exits({position.id})")

    async def _make_decisions(
        self,
        funding_data: Dict[str, Dict[str, float]],
        technical_data: Dict[str, Dict],
        balances: Dict[str, float],
        positions: Dict[str, List[Dict]]
    ) -> List[Dict]:
        """
        Get trading decision from LLM.

        LLM DECIDES EVERYTHING:
        - Whether to trade
        - Direction (LONG/SHORT)
        - Position size (based on conviction)

        We only validate constraints, never force sizes.

        User requirement: "We want it to choose whatever size it wants based on
        its own conviction. And it needs to just have the data."
        """
        decisions = []

        # Get LLM decision
        try:
            llm_decision = await self.llm_engine.get_decision(
                balances=balances,
                positions=positions,
                funding_data=funding_data,
                technical_data=technical_data
            )

            log.log_info(f"LLM DECISION: {llm_decision.get('decision')}")
            log.log_info(f"  Reasoning: {llm_decision.get('reasoning', 'N/A')}")

            if llm_decision.get("decision") == "TRADE":
                symbol = llm_decision.get("symbol")
                direction = llm_decision.get("direction")
                exchange = llm_decision.get("exchange")
                size_usd = llm_decision.get("size_usd", 0)
                conviction = llm_decision.get("conviction", "MEDIUM")

                log.log_info(f"  Symbol: {symbol}")
                log.log_info(f"  Direction: {direction}")
                log.log_info(f"  Exchange: {exchange}")
                log.log_info(f"  Size: ${size_usd:.2f} ({conviction} conviction)")
                log.log_info(f"  Risk Notes: {llm_decision.get('risk_notes', 'None')}")

                # Get technical data for the symbol
                tech = technical_data.get(symbol, {})
                binance_funding = funding_data.get("binance", {}).get(symbol, 0)

                decisions.append({
                    "action": "SWING",
                    "direction": direction,
                    "symbol": symbol,
                    "exchange": exchange,
                    "conviction": conviction,
                    "trade_type": f"SWING_{conviction}",
                    "size_usd": size_usd,
                    "technical_score": tech.get("score", 0),
                    "funding_rate": binance_funding,
                    "reasoning": llm_decision.get("reasoning", ""),
                    "risk_notes": llm_decision.get("risk_notes", ""),
                    "llm_driven": True  # Mark as LLM-driven decision
                })
            else:
                # NO_TRADE decision
                decisions.append({
                    "action": "NO_TRADE",
                    "reasoning": llm_decision.get("reasoning", "LLM decided not to trade"),
                    "risk_notes": llm_decision.get("risk_notes", ""),
                    "llm_driven": True
                })

        except Exception as e:
            log.log_error(e, "_make_decisions (LLM)")
            # Fallback: no trade on error
            decisions.append({
                "action": "NO_TRADE",
                "reasoning": f"LLM error: {str(e)}",
                "llm_driven": False
            })

        return decisions

    def _select_exchange(
        self,
        symbol: str,
        direction: str,
        balances: Dict[str, float]
    ) -> Optional[str]:
        """
        Select the best exchange for this trade.

        Priority:
        1. Has the symbol
        2. Has sufficient balance
        3. Lowest fees
        4. Best liquidity (prefer Paradex for BTC, Hibachi for alts)
        """
        candidates = []

        for exchange, symbols in self.symbols_by_exchange.items():
            if symbol in symbols:
                balance = balances.get(exchange, 0)
                min_order = config.EXCHANGE_CONFIG[exchange]["min_order_usd"]

                if balance >= min_order:
                    # Calculate score based on fees and liquidity
                    fee = config.EXCHANGE_CONFIG[exchange]["maker_fee"]
                    # Prefer lower fees
                    fee_score = 1 - fee * 1000

                    # Prefer specific exchanges for specific assets
                    liquidity_score = 0.5
                    if symbol == "BTC" and exchange == "paradex":
                        liquidity_score = 1.0
                    elif symbol in ["SOL", "SUI", "XRP"] and exchange == "hibachi":
                        liquidity_score = 1.0
                    elif symbol == "XCU" and exchange == "extended":
                        liquidity_score = 1.0
                    elif symbol == "ETH" and exchange == "nado":
                        liquidity_score = 0.8

                    total_score = fee_score + liquidity_score
                    candidates.append((exchange, total_score, balance))

        if not candidates:
            return None

        # Sort by score descending
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    async def _execute_decision(self, decision: Dict) -> None:
        """Execute a trading decision (real or paper)."""
        log.log_decision(decision)

        exchange = decision["exchange"]
        symbol = decision["symbol"]
        direction = decision["direction"]
        size_usd = decision["size_usd"]

        # Get current price from technical engine or agent
        price = None
        if self.technical_engine and symbol in self.technical_engine._cache:
            price = self.technical_engine._cache[symbol].get("price")

        if price is None:
            agent = self.agents.get(exchange)
            if agent:
                try:
                    price = await agent.get_current_price(symbol)
                except:
                    pass

        if not price:
            log.log_error(Exception("Could not get price"), f"_execute_decision({symbol})")
            return

        if self.paper_mode:
            # Simulate trade execution
            await self._execute_paper_trade(decision, price)
        else:
            # Real trade execution
            await self._execute_real_trade(decision, price)

    async def _execute_paper_trade(self, decision: Dict, price: float) -> None:
        """Execute a simulated paper trade."""
        exchange = decision["exchange"]
        symbol = decision["symbol"]
        direction = decision["direction"]
        size_usd = decision["size_usd"]
        trade_type = decision.get("trade_type", "SWING_STANDARD")

        # Create paper position
        position_key = f"{exchange}_{symbol}"
        size = size_usd / price

        self.paper_positions[position_key] = {
            "symbol": symbol,
            "side": direction,
            "size": size,
            "size_usd": size_usd,
            "entry_price": price,
            "current_price": price,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "trade_type": trade_type,
            "unrealized_pnl": 0.0,
            "unrealized_pnl_pct": 0.0
        }

        # Record trade
        trade = {
            "type": "OPEN",
            "exchange": exchange,
            "symbol": symbol,
            "direction": direction,
            "size": size,
            "size_usd": size_usd,
            "price": price,
            "trade_type": trade_type,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        self.paper_trades.append(trade)

        log.log_execution({
            "success": True,
            "exchange": exchange,
            "symbol": symbol,
            "direction": direction,
            "size": size,
            "price": price,
            "order_id": f"PAPER_{position_key}",
            "error": None,
            "paper_trade": True
        })

        # Also track in position manager
        self.position_manager.create_position(
            exchange=exchange,
            symbol=symbol,
            direction=direction,
            entry_price=price,
            size=size,
            size_usd=size_usd,
            trade_type=trade_type
        )

        log.log_info(f"PAPER TRADE OPENED: {direction} {symbol} @ ${price:,.2f} (${size_usd:.2f})")

    async def _execute_real_trade(self, decision: Dict, price: float) -> None:
        """Execute a real trade on exchange."""
        exchange = decision["exchange"]
        symbol = decision["symbol"]
        direction = decision["direction"]
        size_usd = decision["size_usd"]

        agent = self.agents.get(exchange)
        if not agent:
            log.log_error(Exception(f"No agent for {exchange}"), "_execute_real_trade")
            return

        try:
            # Place order
            side = "BUY" if direction == "LONG" else "SELL"
            result = await agent.place_order(
                symbol=symbol,
                side=side,
                size_usd=size_usd,
                order_type="LIMIT",
                price=price
            )

            log.log_execution({
                "success": result.get("success", False),
                "exchange": exchange,
                "symbol": symbol,
                "direction": direction,
                "size": result.get("filled_size", 0),
                "price": result.get("filled_price", price),
                "order_id": result.get("order_id"),
                "error": result.get("error")
            })

            # Create position record if successful
            if result.get("success"):
                self.position_manager.create_position(
                    exchange=exchange,
                    symbol=symbol,
                    direction=direction,
                    entry_price=result.get("filled_price", price),
                    size=result.get("filled_size", size_usd / price),
                    size_usd=size_usd,
                    trade_type=decision.get("trade_type", "SWING_STANDARD")
                )

        except Exception as e:
            log.log_error(e, f"_execute_real_trade({exchange}/{symbol})")

    async def run(self) -> None:
        """Run the orchestrator continuously."""
        self.running = True
        self.start_time = datetime.now(timezone.utc)

        log.log_info("=" * 60)
        log.log_info("SWING ORCHESTRATOR STARTED")
        log.log_info(f"Cycle interval: {config.CYCLE_INTERVAL_SECONDS}s ({config.CYCLE_INTERVAL_SECONDS/60:.0f} min)")
        log.log_info("Decision Engine: LLM-Driven (Qwen)")
        log.log_info("Position Sizing: DYNAMIC (LLM chooses based on conviction)")
        log.log_info("=" * 60)

        while self.running:
            try:
                await self.run_cycle()

                # Generate hourly report
                if self.cycle_count % 4 == 0:  # Every 4 cycles = 1 hour
                    log.generate_hourly_report()

                # Wait for next cycle
                log.log_info(f"Sleeping {config.CYCLE_INTERVAL_SECONDS}s until next cycle...")
                await asyncio.sleep(config.CYCLE_INTERVAL_SECONDS)

            except KeyboardInterrupt:
                log.log_info("Received keyboard interrupt, stopping...")
                self.running = False
            except Exception as e:
                log.log_error(e, "run main loop")
                # Continue running after errors
                await asyncio.sleep(60)  # Short sleep before retry

        log.log_info("SWING ORCHESTRATOR STOPPED")

    def stop(self) -> None:
        """Stop the orchestrator."""
        self.running = False

    async def cleanup(self) -> None:
        """Cleanup resources."""
        if self.funding_monitor:
            await self.funding_monitor.close()
        if self.technical_engine:
            await self.technical_engine.close()
        for agent in self.agents.values():
            if hasattr(agent, 'close'):
                await agent.close()


async def main():
    """Main entry point."""
    orchestrator = SwingOrchestrator()

    try:
        await orchestrator.initialize()
        await orchestrator.run()
    finally:
        await orchestrator.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
