"""
LLM-Driven Decision Engine

The LLM decides everything:
- Whether to trade
- Direction (LONG/SHORT)
- Position size (based on its conviction)

We only provide constraints (exchange minimums, max balance).
The LLM makes the final call.

User requirement: "We want it to choose whatever size it wants based on its own conviction.
And it needs to just have the data. If it has the data, it can do a really good job.
We don't need to force position sizes. As the account goes from $100 to $500,
it dynamically adjusts its strategy."
"""

import os
import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

from . import config

logger = logging.getLogger(__name__)


# LLM Prompt Template for Swing Trading Decisions
SWING_DECISION_PROMPT = """You are an expert swing trader making decisions for a multi-exchange crypto trading bot.

## YOUR TASK
Analyze the market data below and decide whether to open a trade on any asset.
If you decide to trade, YOU choose the position size based on YOUR conviction level.

## ACCOUNT CONTEXT
{account_context}

## MARKET DATA
{market_data}

## EXCHANGE CONSTRAINTS
{exchange_constraints}

## HISTORICAL EDGE
- SHORT trades have 49.4% win rate vs LONG 41.8% (7.6% edge)
- Favor SHORT positions unless strong LONG signal
- Extreme funding rates are contrarian signals:
  - Funding > +0.03%: Crowded longs → SHORT opportunity
  - Funding < -0.03%: Crowded shorts → LONG opportunity

## DECISION GUIDELINES
1. NO_TRADE is always valid - don't force trades
2. If you trade, size based on YOUR conviction (not forced percentages)
3. Higher conviction = larger size (within account limits)
4. Lower conviction = smaller size or no trade
5. Consider: technicals, funding, volatility, existing positions
6. Don't exceed 30% of account on any single trade
7. Be honest about uncertainty

## RESPONSE FORMAT (JSON only, no markdown):
{{
  "decision": "TRADE" or "NO_TRADE",
  "symbol": "BTC" or "ETH" or "SOL" etc (if TRADE),
  "direction": "LONG" or "SHORT" (if TRADE),
  "exchange": "hibachi" or "nado" or "paradex" or "extended" (if TRADE),
  "size_usd": <number> (if TRADE, YOUR chosen size based on conviction),
  "conviction": "HIGH" or "MEDIUM" or "LOW" (if TRADE),
  "reasoning": "<brief explanation of your decision>",
  "risk_notes": "<any concerns or caveats>"
}}

If NO_TRADE, still provide reasoning:
{{
  "decision": "NO_TRADE",
  "reasoning": "<why no trade makes sense right now>",
  "risk_notes": "<market conditions to watch>"
}}

Respond with ONLY the JSON object, no other text."""


class LLMDecisionEngine:
    """
    LLM-driven decision engine.

    The LLM gets full market context and decides:
    - Whether to trade
    - Direction
    - Position size (based on conviction)

    We only validate constraints, never force sizes.
    """

    def __init__(self, api_key: Optional[str] = None, model: str = "qwen-max"):
        """
        Initialize the decision engine.

        Args:
            api_key: OpenRouter API key (falls back to OPEN_ROUTER env var)
            model: Model to use (default: qwen-max for best reasoning)
        """
        self.api_key = api_key or os.getenv("OPEN_ROUTER")
        self.model = model
        self._client = None

        if not self.api_key:
            logger.warning("No API key provided - LLM decisions disabled")

    def _get_client(self):
        """Lazy-load the model client."""
        if self._client is None and self.api_key:
            # Import here to avoid circular imports
            import sys
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from llm_agent.llm.model_client import ModelClient
            self._client = ModelClient(
                api_key=self.api_key,
                model=self.model,
                max_retries=2,
                timeout=90
            )
        return self._client

    def format_account_context(
        self,
        balances: Dict[str, float],
        positions: Dict[str, List[Dict]]
    ) -> str:
        """Format account context for the LLM."""
        lines = ["### Available Balances"]
        total = 0
        for exchange, balance in balances.items():
            lines.append(f"- {exchange}: ${balance:.2f}")
            total += balance
        lines.append(f"- TOTAL AVAILABLE: ${total:.2f}")

        lines.append("\n### Current Positions")
        has_positions = False
        total_unrealized = 0
        for exchange, pos_list in positions.items():
            for pos in pos_list:
                has_positions = True
                symbol = pos.get("symbol", "?")
                side = pos.get("side", "?")
                size_usd = pos.get("size_usd", 0)
                entry = pos.get("entry_price", 0)
                current = pos.get("current_price", entry)
                unrealized = pos.get("unrealized_pnl", 0)
                total_unrealized += unrealized
                lines.append(
                    f"- {exchange}/{symbol}: {side} ${size_usd:.2f} "
                    f"@ ${entry:,.2f} → ${current:,.2f} (P&L: ${unrealized:+.2f})"
                )

        if not has_positions:
            lines.append("- No open positions")
        else:
            lines.append(f"- TOTAL UNREALIZED P&L: ${total_unrealized:+.2f}")

        return "\n".join(lines)

    def format_market_data(
        self,
        funding_data: Dict[str, Dict[str, float]],
        technical_data: Dict[str, Dict]
    ) -> str:
        """Format market data for the LLM."""
        lines = ["### Funding Rates (8h)"]

        # Get Binance funding (most reliable)
        binance_funding = funding_data.get("binance", {})
        for symbol, rate in sorted(binance_funding.items()):
            rate_pct = rate * 100
            signal = ""
            if rate > 0.0003:
                signal = " ⚠️ EXTREME POSITIVE (short squeeze risk)"
            elif rate > 0.0001:
                signal = " [bullish bias]"
            elif rate < -0.0003:
                signal = " ⚠️ EXTREME NEGATIVE (long liquidation risk)"
            elif rate < -0.0001:
                signal = " [bearish bias]"
            lines.append(f"- {symbol}: {rate_pct:+.4f}%{signal}")

        lines.append("\n### Technical Analysis")
        for symbol, tech in sorted(technical_data.items()):
            indicators = tech.get("indicators", {})
            score = tech.get("score", 0)
            direction = tech.get("direction", "NEUTRAL")
            price = indicators.get("price", 0)

            lines.append(f"\n**{symbol}** (Score: {score:.1f}/5.0, Bias: {direction})")
            lines.append(f"  Price: ${price:,.2f}")
            lines.append(f"  RSI(14): {indicators.get('rsi', 50):.1f}")
            lines.append(f"  MACD Histogram: {indicators.get('macd_histogram', 0):.4f}")
            lines.append(f"  OI Change (24h): {indicators.get('oi_change_pct', 0):+.2f}%")
            lines.append(f"  Volume Ratio: {indicators.get('volume_ratio', 1):.2f}x")

            # Add EMA trend if available
            ema_short = indicators.get("ema_short", 0)
            ema_long = indicators.get("ema_long", 0)
            if ema_short and ema_long:
                trend = "BULLISH" if ema_short > ema_long else "BEARISH"
                lines.append(f"  EMA Trend: {trend} (EMA20: ${ema_short:,.2f}, EMA50: ${ema_long:,.2f})")

        return "\n".join(lines)

    def format_exchange_constraints(self) -> str:
        """Format exchange constraints for the LLM."""
        lines = ["### Exchange Minimums & Fees"]

        for exchange, cfg in config.EXCHANGE_CONFIG.items():
            if not cfg.get("enabled", True):
                continue
            min_order = cfg.get("min_order_usd", 10)
            taker_fee = cfg.get("taker_fee", 0) * 100
            maker_fee = cfg.get("maker_fee", 0) * 100
            assets = cfg.get("assets", [])

            asset_list = ", ".join([a.split("-")[0].split("/")[0] for a in assets])
            lines.append(
                f"- {exchange}: min ${min_order}, "
                f"fees {maker_fee:.3f}%/{taker_fee:.3f}% (maker/taker), "
                f"assets: {asset_list}"
            )

        lines.append("\n### Position Limits")
        lines.append(f"- Max positions per exchange: {config.MAX_POSITIONS_PER_EXCHANGE}")
        lines.append(f"- Max total positions: {config.MAX_TOTAL_POSITIONS}")
        lines.append("- Recommended max single trade: 30% of available balance")

        return "\n".join(lines)

    async def get_decision(
        self,
        balances: Dict[str, float],
        positions: Dict[str, List[Dict]],
        funding_data: Dict[str, Dict[str, float]],
        technical_data: Dict[str, Dict]
    ) -> Dict[str, Any]:
        """
        Get a trading decision from the LLM.

        The LLM decides:
        - Whether to trade
        - Which asset/exchange
        - Direction (LONG/SHORT)
        - Position size (based on its conviction)

        We validate constraints but never force sizes.

        Returns:
            Decision dict with keys:
            - decision: "TRADE" or "NO_TRADE"
            - symbol, direction, exchange, size_usd (if TRADE)
            - conviction, reasoning, risk_notes
        """
        client = self._get_client()
        if not client:
            return {
                "decision": "NO_TRADE",
                "reasoning": "LLM not available (no API key)",
                "risk_notes": "Configure OPEN_ROUTER API key"
            }

        # Build the prompt
        account_context = self.format_account_context(balances, positions)
        market_data = self.format_market_data(funding_data, technical_data)
        exchange_constraints = self.format_exchange_constraints()

        prompt = SWING_DECISION_PROMPT.format(
            account_context=account_context,
            market_data=market_data,
            exchange_constraints=exchange_constraints
        )

        # Log the prompt (for debugging)
        logger.debug(f"LLM Decision Prompt:\n{prompt[:500]}...")

        # Query the LLM
        try:
            response = client.query(prompt, max_tokens=800, temperature=0.1)

            if not response or not response.get("content"):
                logger.error("LLM returned empty response")
                return {
                    "decision": "NO_TRADE",
                    "reasoning": "LLM returned empty response",
                    "risk_notes": "Will retry next cycle"
                }

            content = response["content"].strip()
            logger.info(f"LLM raw response: {content[:300]}...")

            # Parse JSON response
            decision = self._parse_response(content)

            # Validate decision
            decision = self._validate_decision(decision, balances, positions)

            return decision

        except Exception as e:
            logger.error(f"LLM decision error: {e}")
            return {
                "decision": "NO_TRADE",
                "reasoning": f"LLM error: {str(e)}",
                "risk_notes": "Will retry next cycle"
            }

    def _parse_response(self, content: str) -> Dict[str, Any]:
        """Parse JSON response from LLM."""
        # Try to extract JSON from response
        content = content.strip()

        # Handle markdown code blocks
        if content.startswith("```"):
            # Remove markdown code block
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
            content = content.strip()

        # Try direct JSON parse
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object in response
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(content[start:end])
            except json.JSONDecodeError:
                pass

        # Couldn't parse - return NO_TRADE
        logger.warning(f"Could not parse LLM response as JSON: {content[:200]}")
        return {
            "decision": "NO_TRADE",
            "reasoning": "Could not parse LLM response",
            "risk_notes": content[:200]
        }

    def _validate_decision(
        self,
        decision: Dict[str, Any],
        balances: Dict[str, float],
        positions: Dict[str, List[Dict]]
    ) -> Dict[str, Any]:
        """
        Validate and adjust decision based on constraints.

        We DON'T force sizes - we only reject invalid trades.
        The LLM's chosen size is respected if valid.
        """
        if decision.get("decision") != "TRADE":
            return decision

        exchange = decision.get("exchange")
        symbol = decision.get("symbol")
        size_usd = decision.get("size_usd", 0)

        # Check exchange is valid
        if exchange not in config.EXCHANGE_CONFIG:
            decision["decision"] = "NO_TRADE"
            decision["reasoning"] = f"Invalid exchange: {exchange}"
            return decision

        # Check exchange is enabled
        if not config.EXCHANGE_CONFIG[exchange].get("enabled", True):
            decision["decision"] = "NO_TRADE"
            decision["reasoning"] = f"Exchange {exchange} is disabled"
            return decision

        # Check symbol is available on exchange
        available_assets = config.EXCHANGE_CONFIG[exchange].get("assets", [])
        symbol_found = any(symbol in asset for asset in available_assets)
        if not symbol_found:
            decision["decision"] = "NO_TRADE"
            decision["reasoning"] = f"{symbol} not available on {exchange}"
            return decision

        # Check minimum order size
        min_order = config.EXCHANGE_CONFIG[exchange].get("min_order_usd", 10)
        if size_usd < min_order:
            decision["decision"] = "NO_TRADE"
            decision["reasoning"] = (
                f"LLM chose ${size_usd:.2f} but {exchange} minimum is ${min_order}. "
                f"Respecting LLM's low conviction - not overriding to minimum."
            )
            return decision

        # Check available balance
        available = balances.get(exchange, 0)
        if size_usd > available:
            # Adjust to available (LLM may not have exact balance info)
            old_size = size_usd
            decision["size_usd"] = available * 0.95  # Leave 5% buffer
            decision["risk_notes"] = (
                f"{decision.get('risk_notes', '')} "
                f"[Adjusted from ${old_size:.2f} to ${decision['size_usd']:.2f} due to balance]"
            ).strip()
            logger.info(f"Adjusted size from ${old_size:.2f} to ${decision['size_usd']:.2f}")

        # Check position limits
        exchange_positions = len(positions.get(exchange, []))
        if exchange_positions >= config.MAX_POSITIONS_PER_EXCHANGE:
            decision["decision"] = "NO_TRADE"
            decision["reasoning"] = f"Max positions ({config.MAX_POSITIONS_PER_EXCHANGE}) reached on {exchange}"
            return decision

        total_positions = sum(len(p) for p in positions.values())
        if total_positions >= config.MAX_TOTAL_POSITIONS:
            decision["decision"] = "NO_TRADE"
            decision["reasoning"] = f"Max total positions ({config.MAX_TOTAL_POSITIONS}) reached"
            return decision

        # Check for duplicate position
        for pos in positions.get(exchange, []):
            if symbol in pos.get("symbol", ""):
                decision["decision"] = "NO_TRADE"
                decision["reasoning"] = f"Already have position in {symbol} on {exchange}"
                return decision

        # Check blocked assets
        if symbol in config.BLOCKED_ASSETS:
            decision["decision"] = "NO_TRADE"
            decision["reasoning"] = f"{symbol} is blocked (poor historical performance)"
            return decision

        return decision


# Singleton instance
_engine: Optional[LLMDecisionEngine] = None


def get_engine() -> LLMDecisionEngine:
    """Get the singleton decision engine."""
    global _engine
    if _engine is None:
        _engine = LLMDecisionEngine()
    return _engine
