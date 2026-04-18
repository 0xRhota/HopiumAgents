"""
Technical Analysis Engine

Calculates 5-signal score for swing trading decisions.
Uses Binance data as reference for indicators.
"""

import asyncio
import aiohttp
import pandas as pd
import numpy as np
from typing import Dict, Optional, List
from datetime import datetime, timezone

from . import config
from . import logger as log


class TechnicalEngine:
    """
    Calculates technical indicators and generates trading scores.

    5-Signal Scoring System:
    1. RSI Signal (0-1): Extreme overbought/oversold
    2. MACD Signal (0-1): Crossover or strong histogram
    3. OI Signal (0-1): OI divergence with price
    4. Volume Signal (0-1): Volume spike vs average
    5. Trend Signal (0-1): Price vs EMA alignment

    Score Interpretation:
    - <2.5: NO_TRADE
    - 2.5-3.0: Tier 1 only (BTC, ETH)
    - 3.0-4.0: Standard swing
    - >4.0: High conviction (scalp allowed)
    """

    # Binance symbol mapping
    BINANCE_SYMBOLS = {
        "BTC": "BTCUSDT",
        "ETH": "ETHUSDT",
        "SOL": "SOLUSDT",
        "SUI": "SUIUSDT",
        "XRP": "XRPUSDT",
    }

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self._cache: Dict[str, Dict] = {}

    async def _ensure_session(self) -> None:
        """Ensure aiohttp session exists."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close(self) -> None:
        """Close the session."""
        if self.session and not self.session.closed:
            await self.session.close()

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 100
    ) -> Optional[pd.DataFrame]:
        """
        Get OHLCV data from Binance Futures.

        Args:
            symbol: Asset symbol (e.g., "BTC")
            interval: Kline interval (1m, 5m, 15m, 1h, 4h, 1d)
            limit: Number of candles

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
        """
        await self._ensure_session()

        binance_symbol = self.BINANCE_SYMBOLS.get(symbol)
        if not binance_symbol:
            log.log_debug(f"No Binance mapping for {symbol}")
            return None

        try:
            url = f"https://fapi.binance.com/fapi/v1/klines?symbol={binance_symbol}&interval={interval}&limit={limit}"
            async with self.session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    df = pd.DataFrame(data, columns=[
                        'timestamp', 'open', 'high', 'low', 'close', 'volume',
                        'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                        'taker_buy_quote', 'ignore'
                    ])
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                    for col in ['open', 'high', 'low', 'close', 'volume']:
                        df[col] = df[col].astype(float)
                    return df
        except Exception as e:
            log.log_error(e, f"get_klines({symbol})")

        return None

    async def get_open_interest(self, symbol: str) -> Optional[Dict]:
        """
        Get open interest data from Binance Futures.

        Returns:
            {"oi": float, "oi_change_pct": float}
        """
        await self._ensure_session()

        binance_symbol = self.BINANCE_SYMBOLS.get(symbol)
        if not binance_symbol:
            return None

        try:
            # Current OI
            url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={binance_symbol}"
            async with self.session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    current_oi = float(data.get("openInterest", 0))

            # Historical OI (for change calculation)
            url = f"https://fapi.binance.com/futures/data/openInterestHist?symbol={binance_symbol}&period=1h&limit=2"
            async with self.session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if len(data) >= 2:
                        prev_oi = float(data[-2].get("sumOpenInterest", current_oi))
                        oi_change_pct = ((current_oi - prev_oi) / prev_oi) * 100 if prev_oi > 0 else 0
                        return {
                            "oi": current_oi,
                            "oi_change_pct": oi_change_pct
                        }

            return {"oi": current_oi, "oi_change_pct": 0}

        except Exception as e:
            log.log_error(e, f"get_open_interest({symbol})")
            return None

    def calculate_rsi(self, prices: pd.Series, period: int = 14) -> float:
        """Calculate RSI."""
        delta = prices.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)

        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 50.0

    def calculate_macd(
        self,
        prices: pd.Series,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9
    ) -> Dict[str, float]:
        """Calculate MACD."""
        ema_fast = prices.ewm(span=fast, adjust=False).mean()
        ema_slow = prices.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line

        return {
            "value": macd_line.iloc[-1],
            "signal": signal_line.iloc[-1],
            "histogram": histogram.iloc[-1]
        }

    def calculate_ema(self, prices: pd.Series, period: int) -> float:
        """Calculate EMA."""
        return prices.ewm(span=period, adjust=False).mean().iloc[-1]

    def calculate_volume_ratio(self, volumes: pd.Series) -> float:
        """Calculate current volume vs 24h average."""
        avg_volume = volumes.iloc[:-1].mean()
        current_volume = volumes.iloc[-1]
        return current_volume / avg_volume if avg_volume > 0 else 1.0

    async def get_indicators(self, symbol: str) -> Optional[Dict]:
        """
        Get all technical indicators for a symbol.

        Returns:
            {
                "rsi": float,
                "macd": {"value": float, "signal": float, "histogram": float},
                "oi_change_pct": float,
                "volume_ratio": float,
                "ema_short": float,
                "ema_long": float,
                "price": float,
                "price_change_1h_pct": float
            }
        """
        # Get klines
        df = await self.get_klines(symbol, interval="1h", limit=100)
        if df is None or len(df) < 50:
            log.log_warning(f"Insufficient data for {symbol}")
            return None

        # Get OI
        oi_data = await self.get_open_interest(symbol)

        prices = df['close']
        volumes = df['volume']
        current_price = prices.iloc[-1]
        prev_price = prices.iloc[-2]

        indicators = {
            "rsi": self.calculate_rsi(prices, config.RSI_PERIOD),
            "macd": self.calculate_macd(prices, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL),
            "oi_change_pct": oi_data.get("oi_change_pct", 0) if oi_data else 0,
            "volume_ratio": self.calculate_volume_ratio(volumes),
            "ema_short": self.calculate_ema(prices, config.EMA_SHORT),
            "ema_long": self.calculate_ema(prices, config.EMA_LONG),
            "price": current_price,
            "price_change_1h_pct": ((current_price - prev_price) / prev_price) * 100 if prev_price > 0 else 0
        }

        self._cache[symbol] = indicators
        return indicators

    def calculate_score(self, symbol: str, indicators: Dict) -> Dict:
        """
        Calculate 5-signal trading score.

        Args:
            symbol: Asset symbol
            indicators: Output from get_indicators()

        Returns:
            {
                "score": float (0-5),
                "direction": "LONG" | "SHORT",
                "signals": {
                    "rsi": {"value": float, "score": float, "direction": str},
                    "macd": {...},
                    "oi": {...},
                    "volume": {...},
                    "trend": {...}
                }
            }
        """
        signals = {}
        total_score = 0.0
        long_votes = 0
        short_votes = 0

        # 1. RSI Signal (0-1 points)
        rsi = indicators.get("rsi", 50)
        rsi_score = 0
        rsi_direction = "NEUTRAL"

        if rsi < config.RSI_OVERSOLD:
            rsi_score = 1.0
            rsi_direction = "LONG"
            long_votes += 1
        elif rsi > config.RSI_OVERBOUGHT:
            rsi_score = 1.0
            rsi_direction = "SHORT"
            short_votes += 1
        elif rsi < 40:
            rsi_score = 0.5
            rsi_direction = "LONG"
            long_votes += 0.5
        elif rsi > 60:
            rsi_score = 0.5
            rsi_direction = "SHORT"
            short_votes += 0.5

        signals["rsi"] = {"value": rsi, "score": rsi_score, "direction": rsi_direction}
        total_score += rsi_score

        # 2. MACD Signal (0-1 points)
        macd = indicators.get("macd", {})
        histogram = macd.get("histogram", 0)
        macd_value = macd.get("value", 0)
        macd_signal = macd.get("signal", 0)

        macd_score = 0
        macd_direction = "NEUTRAL"

        # Check for crossover or strong histogram
        if histogram > 0 and macd_value > macd_signal:
            macd_score = 0.5 if abs(histogram) > 0 else 0
            macd_direction = "LONG"
            long_votes += 0.5
            # Bonus for strong histogram
            if abs(histogram) > abs(macd_value) * 0.1:
                macd_score = 1.0
                long_votes += 0.5
        elif histogram < 0 and macd_value < macd_signal:
            macd_score = 0.5 if abs(histogram) > 0 else 0
            macd_direction = "SHORT"
            short_votes += 0.5
            if abs(histogram) > abs(macd_value) * 0.1:
                macd_score = 1.0
                short_votes += 0.5

        signals["macd"] = {"value": histogram, "score": macd_score, "direction": macd_direction}
        total_score += macd_score

        # 3. OI Signal (0-1 points) - Divergence detection
        oi_change = indicators.get("oi_change_pct", 0)
        price_change = indicators.get("price_change_1h_pct", 0)

        oi_score = 0
        oi_direction = "NEUTRAL"

        # OI divergence: price up + OI down = bearish, price down + OI up = bullish
        if price_change > 0.5 and oi_change < -2:
            # Price up but OI down = weak rally, expect reversal
            oi_score = 0.75
            oi_direction = "SHORT"
            short_votes += 0.75
        elif price_change < -0.5 and oi_change > 2:
            # Price down but OI up = weak selloff, expect bounce
            oi_score = 0.75
            oi_direction = "LONG"
            long_votes += 0.75
        elif abs(oi_change) > 5:
            # Large OI change = something happening
            oi_score = 0.5
            oi_direction = "LONG" if oi_change > 0 else "SHORT"
            if oi_change > 0:
                long_votes += 0.5
            else:
                short_votes += 0.5

        signals["oi"] = {"value": oi_change, "score": oi_score, "direction": oi_direction}
        total_score += oi_score

        # 4. Volume Signal (0-1 points)
        volume_ratio = indicators.get("volume_ratio", 1)

        volume_score = 0
        volume_direction = "NEUTRAL"

        if volume_ratio > config.VOLUME_SPIKE_MULTIPLIER:
            volume_score = 1.0
            # Volume spike direction follows price direction
            if price_change > 0:
                volume_direction = "LONG"
                long_votes += 1
            else:
                volume_direction = "SHORT"
                short_votes += 1
        elif volume_ratio > 1.5:
            volume_score = 0.5
            if price_change > 0:
                volume_direction = "LONG"
                long_votes += 0.5
            else:
                volume_direction = "SHORT"
                short_votes += 0.5

        signals["volume"] = {"value": volume_ratio, "score": volume_score, "direction": volume_direction}
        total_score += volume_score

        # 5. Trend Signal (0-1 points) - EMA alignment
        price = indicators.get("price", 0)
        ema_short = indicators.get("ema_short", price)
        ema_long = indicators.get("ema_long", price)

        trend_score = 0
        trend_direction = "NEUTRAL"

        if price > ema_short > ema_long:
            # Strong uptrend
            trend_score = 1.0
            trend_direction = "LONG"
            long_votes += 1
        elif price < ema_short < ema_long:
            # Strong downtrend
            trend_score = 1.0
            trend_direction = "SHORT"
            short_votes += 1
        elif price > ema_short and price > ema_long:
            # Above both EMAs
            trend_score = 0.5
            trend_direction = "LONG"
            long_votes += 0.5
        elif price < ema_short and price < ema_long:
            # Below both EMAs
            trend_score = 0.5
            trend_direction = "SHORT"
            short_votes += 0.5

        signals["trend"] = {"value": f"P:{price:.0f} S:{ema_short:.0f} L:{ema_long:.0f}", "score": trend_score, "direction": trend_direction}
        total_score += trend_score

        # Determine overall direction
        overall_direction = "LONG" if long_votes > short_votes else "SHORT" if short_votes > long_votes else "NEUTRAL"

        return {
            "score": total_score,
            "direction": overall_direction,
            "long_votes": long_votes,
            "short_votes": short_votes,
            "signals": signals
        }

    async def analyze(self, symbol: str) -> Optional[Dict]:
        """
        Full analysis for a symbol: indicators + score.

        Returns:
            {
                "symbol": str,
                "indicators": {...},
                "score": float,
                "direction": str,
                "signals": {...}
            }
        """
        indicators = await self.get_indicators(symbol)
        if indicators is None:
            return None

        score_data = self.calculate_score(symbol, indicators)

        return {
            "symbol": symbol,
            "indicators": indicators,
            **score_data
        }


# Global instance
_engine: Optional[TechnicalEngine] = None


def get_engine() -> TechnicalEngine:
    """Get or create the global technical engine."""
    global _engine
    if _engine is None:
        _engine = TechnicalEngine()
    return _engine


async def analyze_symbol(symbol: str) -> Optional[Dict]:
    """Convenience function to analyze a single symbol."""
    engine = get_engine()
    return await engine.analyze(symbol)


async def analyze_all_symbols() -> Dict[str, Dict]:
    """Analyze all tracked symbols."""
    engine = get_engine()
    results = {}

    symbols = list(TechnicalEngine.BINANCE_SYMBOLS.keys())

    for symbol in symbols:
        try:
            analysis = await engine.analyze(symbol)
            if analysis:
                results[symbol] = analysis
        except Exception as e:
            log.log_error(e, f"analyze_all_symbols - {symbol}")

    return results
