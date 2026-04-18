"""
Simplified GPT-like Decision Engine - Multi-Timeframe Version

Based on Moon Dev AI Agents pattern:
1. Multi-timeframe analysis (1h, 4h, 1d)
2. Boolean signal matrix for clarity
3. Crowd positioning data
4. Contrarian rules
5. Fear & Greed sentiment
"""

import os
import json
import logging
import requests
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

from . import config

logger = logging.getLogger(__name__)


# Multi-timeframe prompt with contrarian rules
SIMPLIFIED_DECISION_PROMPT = """You are a swing trader. Analyze this multi-timeframe data and decide.

## MARKET DATA
{market_data}

## CONTRARIAN RULES
- Extreme Fear (<20) + Daily RSI Oversold (<30) = STRONG BUY signal
- Extreme Greed (>80) + Daily RSI Overbought (>70) = STRONG SELL signal
- Crowd heavily long (>60%) + price dumping = wait for capitulation or buy
- Crowd heavily short (>60%) + price pumping = short squeeze, buy
- Use DAILY timeframe for direction, 4H for entry timing

## ACCOUNT
{account_context}

## RULES
- Score -2: Strong SHORT (max ${max_trade_size:.0f})
- Score -1: Mild SHORT (${half_trade_size:.0f})
- Score 0: NO_TRADE (wait for clarity)
- Score +1: Mild LONG (${half_trade_size:.0f})
- Score +2: Strong LONG (max ${max_trade_size:.0f})

## CONSTRAINTS
- Min order: ${min_order:.0f}
- Exchange: {exchanges}

## RESPONSE (JSON only)
{{"score": <-2 to +2>, "action": "BUY" or "SELL" or "NO_TRADE", "symbol": "<symbol>", "exchange": "<exchange>", "size_usd": <number>, "tp": <price>, "sl": <price>, "reason": "<15 words max>"}}

JSON ONLY."""


class SimplifiedDecisionEngine:
    """Multi-timeframe decision engine with full market context."""

    def __init__(self, api_key: Optional[str] = None, model: str = "qwen-max"):
        self.model = self._normalize_model_name(model)

        if api_key:
            self.api_key = api_key
        elif "gpt-5" in self.model:
            self.api_key = os.getenv("OPENAI_API_KEY")
        else:
            self.api_key = os.getenv("OPEN_ROUTER")

        self._client = None
        if not self.api_key:
            logger.warning("No API key provided - decisions disabled")

    def _normalize_model_name(self, model: str) -> str:
        # Maps user-facing aliases to ModelClient MODEL_CONFIGS keys.
        model_map = {
            "gpt": "gpt-5.1-instant",
            "gpt-5": "gpt-5.1-instant",
            "gpt5": "gpt-5.1-instant",
            "gpt-5.1": "gpt-5.1-instant",
            "qwen": "qwen-max",
            "qwen-max": "qwen-max",
            "qwen-2.5-72b": "qwen-max",
            "qwen-free": "qwen-free",
            "qwen3": "qwen-max",
            "qwen3.6": "qwen-max",
            "qwen/qwen3.6-plus:free": "qwen-free",
        }
        return model_map.get(model.lower(), model)

    def _get_client(self):
        if self._client is None and self.api_key:
            import sys
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from llm_agent.llm.model_client import ModelClient
            self._client = ModelClient(
                api_key=self.api_key,
                model=self.model,
                max_retries=2,
                timeout=60
            )
        return self._client

    def _fetch_binance_klines(self, symbol: str, interval: str, limit: int = 50) -> List:
        """Fetch klines from Binance."""
        try:
            pair = f"{symbol}USDT"
            resp = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": pair, "interval": interval, "limit": limit},
                timeout=10
            )
            return resp.json() if resp.status_code == 200 else []
        except Exception as e:
            logger.error(f"Error fetching klines: {e}")
            return []

    def _calc_indicators(self, klines: List) -> Dict:
        """Calculate technical indicators from klines."""
        if not klines or len(klines) < 26:
            return {}

        closes = [float(k[4]) for k in klines]
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        volumes = [float(k[5]) for k in klines]

        price = closes[-1]

        # RSI
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas[-14:]]
        losses = [-d if d < 0 else 0 for d in deltas[-14:]]
        avg_gain = sum(gains) / 14
        avg_loss = sum(losses) / 14
        rsi = 100 - (100 / (1 + avg_gain / (avg_loss + 0.0001)))

        # MACD
        ema12 = sum(closes[-12:]) / 12
        ema26 = sum(closes[-26:]) / 26
        macd = ema12 - ema26

        # MAs
        ma20 = sum(closes[-20:]) / 20
        ma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else ma20

        # Donchian
        dcu = max(highs[-20:])
        dcl = min(lows[-20:])
        don_pos = ((price - dcl) / (dcu - dcl)) * 100 if dcu != dcl else 50

        # Volume
        vol_avg = sum(volumes[-20:]) / 20
        vol_ratio = volumes[-1] / vol_avg if vol_avg > 0 else 1

        # Change
        change = ((closes[-1] - closes[0]) / closes[0]) * 100

        return {
            "price": price,
            "rsi": rsi,
            "macd": macd,
            "ma20": ma20,
            "ma50": ma50,
            "don_upper": dcu,
            "don_lower": dcl,
            "don_pos": don_pos,
            "vol_ratio": vol_ratio,
            "change": change,
            # Boolean signals (Moon Dev style)
            "price_above_ma20": price > ma20,
            "price_above_ma50": price > ma50,
            "ma20_above_ma50": ma20 > ma50,
            "rsi_oversold": rsi < 30,
            "rsi_overbought": rsi > 70,
            "macd_bullish": macd > 0,
            "near_support": don_pos < 30,
            "near_resistance": don_pos > 70,
        }

    def _fetch_crowd_data(self, symbol: str = "BTCUSDT") -> Dict:
        """Fetch crowd positioning from Binance Futures."""
        try:
            # Long/Short Ratio
            ls_resp = requests.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": symbol, "period": "1h", "limit": 1},
                timeout=5
            )
            ls_data = ls_resp.json()[0] if ls_resp.status_code == 200 else {}

            # Top Traders
            top_resp = requests.get(
                "https://fapi.binance.com/futures/data/topLongShortPositionRatio",
                params={"symbol": symbol, "period": "1h", "limit": 1},
                timeout=5
            )
            top_data = top_resp.json()[0] if top_resp.status_code == 200 else {}

            # Taker Buy/Sell
            taker_resp = requests.get(
                "https://fapi.binance.com/futures/data/takerlongshortRatio",
                params={"symbol": symbol, "period": "1h", "limit": 1},
                timeout=5
            )
            taker_data = taker_resp.json()[0] if taker_resp.status_code == 200 else {}

            # Open Interest
            oi_resp = requests.get(
                "https://fapi.binance.com/fapi/v1/openInterest",
                params={"symbol": symbol},
                timeout=5
            )
            oi_data = oi_resp.json() if oi_resp.status_code == 200 else {}

            # Funding Rate
            funding_resp = requests.get(
                "https://fapi.binance.com/fapi/v1/premiumIndex",
                params={"symbol": symbol},
                timeout=5
            )
            funding_data = funding_resp.json() if funding_resp.status_code == 200 else {}

            return {
                "long_ratio": float(ls_data.get("longShortRatio", 1)),
                "long_pct": float(ls_data.get("longAccount", 0.5)) * 100,
                "top_long_ratio": float(top_data.get("longShortRatio", 1)),
                "top_long_pct": float(top_data.get("longAccount", 0.5)) * 100,
                "taker_ratio": float(taker_data.get("buySellRatio", 1)),
                "open_interest": float(oi_data.get("openInterest", 0)),
                "funding_rate": float(funding_data.get("lastFundingRate", 0)) * 100,
            }
        except Exception as e:
            logger.error(f"Error fetching crowd data: {e}")
            return {}

    def _fetch_fear_greed(self) -> Dict:
        """Fetch Fear & Greed Index."""
        try:
            resp = requests.get("https://api.alternative.me/fng/", timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data", [{}])[0]
                return {
                    "value": int(data.get("value", 50)),
                    "classification": data.get("value_classification", "Neutral")
                }
        except Exception as e:
            logger.error(f"Error fetching Fear & Greed: {e}")
        return {"value": 50, "classification": "Neutral"}

    def format_market_data(
        self,
        funding_data: Dict[str, Dict[str, float]],
        technical_data: Dict[str, Dict]
    ) -> str:
        """Format market data with full multi-timeframe context."""
        lines = []

        # Get crowd data and sentiment
        crowd = self._fetch_crowd_data()
        fng = self._fetch_fear_greed()

        for symbol in sorted(technical_data.keys()):
            # Fetch multi-timeframe data
            tf_data = {}
            for interval in ["1h", "4h", "1d"]:
                klines = self._fetch_binance_klines(symbol, interval)
                if klines:
                    tf_data[interval] = self._calc_indicators(klines)

            if not tf_data.get("1d"):
                continue

            d1 = tf_data.get("1d", {})
            d4 = tf_data.get("4h", {})
            d1h = tf_data.get("1h", {})

            lines.append(f"## {symbol}")
            lines.append(f"**Price: ${d1.get('price', 0):,.0f}**")
            lines.append("")

            # Signal Matrix (Moon Dev style)
            lines.append("### SIGNAL MATRIX")
            lines.append("| Signal | 1H | 4H | 1D |")
            lines.append("|--------|----|----|----| ")

            signals = [
                ("Price > MA20", "price_above_ma20"),
                ("Price > MA50", "price_above_ma50"),
                ("MACD Bullish", "macd_bullish"),
                ("RSI Oversold", "rsi_oversold"),
                ("RSI Overbought", "rsi_overbought"),
                ("Near Support", "near_support"),
                ("Near Resistance", "near_resistance"),
            ]

            for label, key in signals:
                v1h = "✅" if d1h.get(key) else "❌"
                v4h = "✅" if d4.get(key) else "❌"
                v1d = "✅" if d1.get(key) else "❌"
                lines.append(f"| {label} | {v1h} | {v4h} | {v1d} |")

            lines.append("")

            # Raw values
            lines.append("### RAW VALUES")
            lines.append("| TF | RSI | MACD | Donchian Pos |")
            lines.append("|----|-----|------|--------------|")
            lines.append(f"| 1H | {d1h.get('rsi', 50):.0f} | {d1h.get('macd', 0):+.0f} | {d1h.get('don_pos', 50):.0f}% |")
            lines.append(f"| 4H | {d4.get('rsi', 50):.0f} | {d4.get('macd', 0):+.0f} | {d4.get('don_pos', 50):.0f}% |")
            lines.append(f"| 1D | {d1.get('rsi', 50):.0f} | {d1.get('macd', 0):+.0f} | {d1.get('don_pos', 50):.0f}% |")
            lines.append("")

            # Key levels
            lines.append("### KEY LEVELS")
            lines.append(f"- Daily MA20: ${d1.get('ma20', 0):,.0f}")
            lines.append(f"- Daily MA50: ${d1.get('ma50', 0):,.0f}")
            lines.append(f"- Daily Donchian: ${d1.get('don_lower', 0):,.0f} - ${d1.get('don_upper', 0):,.0f}")
            lines.append("")

        # Crowd & Sentiment section
        lines.append("## CROWD & SENTIMENT")
        lines.append(f"- Retail L/S: {crowd.get('long_ratio', 1):.2f} ({crowd.get('long_pct', 50):.0f}% long)")
        lines.append(f"- Top Traders: {crowd.get('top_long_ratio', 1):.2f} ({crowd.get('top_long_pct', 50):.0f}% long)")
        lines.append(f"- Taker Flow: {crowd.get('taker_ratio', 1):.2f} ({'BUYERS' if crowd.get('taker_ratio', 1) > 1 else 'SELLERS'})")
        lines.append(f"- Open Interest: {crowd.get('open_interest', 0):,.0f} BTC")
        lines.append(f"- Funding Rate: {crowd.get('funding_rate', 0):.4f}%")
        lines.append(f"- **Fear & Greed: {fng['value']} ({fng['classification']})**")

        return "\n".join(lines)

    def format_account_context(
        self,
        balances: Dict[str, float],
        positions: Dict[str, List[Dict]]
    ) -> str:
        total = sum(balances.values())
        pos_summary = []
        for exchange, pos_list in positions.items():
            for pos in pos_list:
                symbol = pos.get("symbol", "?")
                side = pos.get("side", "?")
                pnl = pos.get("unrealized_pnl", 0)
                pos_summary.append(f"{exchange}/{symbol}: {side} (${pnl:+.2f})")
        return f"Balance: ${total:.2f} | Positions: {', '.join(pos_summary) if pos_summary else 'None'}"

    async def get_decision(
        self,
        balances: Dict[str, float],
        positions: Dict[str, List[Dict]],
        funding_data: Dict[str, Dict[str, float]],
        technical_data: Dict[str, Dict]
    ) -> Dict[str, Any]:
        """Get trading decision with full market context."""
        client = self._get_client()
        if not client:
            return {"decision": "NO_TRADE", "reasoning": "No API key"}

        total_balance = sum(balances.values())
        min_order = 10  # Paradex minimum notional

        # Calculate trade size, but never below minimum
        max_trade_size = min(total_balance * 0.25, 50)
        max_trade_size = max(max_trade_size, min_order)  # Enforce minimum
        half_trade_size = max(max_trade_size / 2, min_order)  # Half size also respects minimum

        # If balance can't support minimum order, don't trade
        if total_balance < min_order * 1.5:  # Need some buffer
            return {"decision": "NO_TRADE", "reasoning": f"Balance ${total_balance:.2f} too low for min ${min_order} order"}

        available_exchanges = [
            ex for ex, bal in balances.items()
            if bal >= config.EXCHANGE_CONFIG.get(ex, {}).get("min_order_usd", 10)
        ]

        if not available_exchanges:
            return {"decision": "NO_TRADE", "reasoning": "Insufficient balance"}

        account_context = self.format_account_context(balances, positions)
        market_data = self.format_market_data(funding_data, technical_data)

        prompt = SIMPLIFIED_DECISION_PROMPT.format(
            account_context=account_context,
            market_data=market_data,
            max_trade_size=max_trade_size,
            half_trade_size=half_trade_size,
            min_order=min_order,
            exchanges=", ".join(available_exchanges)
        )

        try:
            response = client.query(prompt, max_tokens=400, temperature=0.2)

            if not response or not response.get("content"):
                return {"decision": "NO_TRADE", "reasoning": "Empty response"}

            content = response["content"].strip()
            logger.info(f"Simplified engine response: {content[:300]}")

            return self._parse_response(content, balances, technical_data)

        except Exception as e:
            logger.error(f"Simplified engine error: {e}")
            return {"decision": "NO_TRADE", "reasoning": f"Error: {str(e)}"}

    def _parse_response(
        self,
        content: str,
        balances: Dict[str, float],
        technical_data: Dict[str, Dict]
    ) -> Dict[str, Any]:
        """Parse and validate LLM response."""
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(content[start:end])
            else:
                return {"decision": "NO_TRADE", "reasoning": "No JSON found"}
        except json.JSONDecodeError:
            return {"decision": "NO_TRADE", "reasoning": "Invalid JSON"}

        if data.get("action") == "NO_TRADE" or data.get("score", 0) == 0:
            return {"decision": "NO_TRADE", "reasoning": data.get("reason", "Score 0")}

        symbol = data.get("symbol", "").replace("-USD-PERP", "").replace("/USD", "").replace("USDT", "")
        exchange = data.get("exchange")
        action = data.get("action")
        size_usd = data.get("size_usd", 0)
        score = data.get("score", 0)

        # Enforce minimum order size
        min_order = 10
        if size_usd < min_order:
            size_usd = min_order
            logger.info(f"Size ${data.get('size_usd', 0)} below minimum, adjusted to ${min_order}")

        if not all([symbol, exchange, action, size_usd]):
            return {"decision": "NO_TRADE", "reasoning": "Missing fields"}

        if exchange not in balances or balances[exchange] < size_usd:
            for ex, bal in balances.items():
                if bal >= size_usd:
                    exchange = ex
                    break
            else:
                return {"decision": "NO_TRADE", "reasoning": "Insufficient balance"}

        price = technical_data.get(symbol, {}).get("indicators", {}).get("price", 0)
        if not price:
            # Try to get price from Binance
            try:
                resp = requests.get(
                    "https://api.binance.com/api/v3/ticker/price",
                    params={"symbol": f"{symbol}USDT"},
                    timeout=5
                )
                if resp.status_code == 200:
                    price = float(resp.json().get("price", 0))
            except:
                pass

        if not price:
            return {"decision": "NO_TRADE", "reasoning": f"No price for {symbol}"}

        direction = "LONG" if action == "BUY" else "SHORT"

        # Use LLM's TP/SL if provided, otherwise calculate
        tp = data.get("tp")
        sl = data.get("sl")

        if not tp or not sl:
            if direction == "LONG":
                tp = price * 1.05  # +5%
                sl = price * 0.97  # -3%
            else:
                tp = price * 0.95  # -5%
                sl = price * 1.03  # +3%

        conviction_map = {-2: "HIGH", -1: "MEDIUM", 1: "MEDIUM", 2: "HIGH"}
        conviction = conviction_map.get(score, "LOW")

        return {
            "decision": "TRADE",
            "symbol": symbol,
            "direction": direction,
            "exchange": exchange,
            "size_usd": size_usd,
            "conviction": conviction,
            "score": score,
            "tp": tp,
            "sl": sl,
            "reasoning": data.get("reason", f"Score {score}"),
            "risk_notes": f"TP: ${tp:,.2f}, SL: ${sl:,.2f}"
        }


def create_engine(engine_type: str = "current", model: str = "qwen") -> Any:
    """Create a decision engine."""
    if engine_type == "simplified":
        return SimplifiedDecisionEngine(model=model)
    else:
        from .llm_decision_engine import LLMDecisionEngine
        model_name = "qwen/qwen-2.5-72b-instruct" if "qwen" in model.lower() else "openai/gpt-4o"
        return LLMDecisionEngine(model=model_name)
