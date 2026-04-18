"""
Funding Rate Monitor

Tracks funding rates from Binance (reference) and all DEXes.
Generates trading signals based on funding extremes.
"""

import asyncio
import aiohttp
from typing import Dict, Optional, Tuple
from datetime import datetime, timezone

from . import config
from . import logger as log


class FundingMonitor:
    """
    Monitors funding rates across exchanges.

    Funding Rate Zones:
    - Extreme Positive (>+0.03%): STRONG LONG signal (short squeeze potential)
    - Moderate Positive (+0.01% to +0.03%): WEAK LONG signal
    - Neutral (-0.01% to +0.01%): No signal
    - Moderate Negative (-0.03% to -0.01%): WEAK SHORT signal
    - Extreme Negative (<-0.03%): STRONG SHORT signal (long liquidations)
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
        self._cache_time: Optional[datetime] = None

    async def _ensure_session(self) -> None:
        """Ensure aiohttp session exists."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()

    async def close(self) -> None:
        """Close the session."""
        if self.session and not self.session.closed:
            await self.session.close()

    async def get_binance_funding(self, symbol: str) -> Optional[float]:
        """
        Get funding rate from Binance Futures.
        Returns funding rate as decimal (0.0001 = 0.01%)
        """
        await self._ensure_session()

        binance_symbol = self.BINANCE_SYMBOLS.get(symbol)
        if not binance_symbol:
            log.log_debug(f"No Binance mapping for {symbol}")
            return None

        try:
            url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={binance_symbol}&limit=1"
            async with self.session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        return float(data[0]["fundingRate"])
        except Exception as e:
            log.log_error(e, f"get_binance_funding({symbol})")

        return None

    async def get_all_binance_funding(self) -> Dict[str, float]:
        """Get funding rates for all tracked symbols from Binance."""
        results = {}

        tasks = {
            symbol: self.get_binance_funding(symbol)
            for symbol in self.BINANCE_SYMBOLS.keys()
        }

        for symbol, task in tasks.items():
            try:
                rate = await task
                if rate is not None:
                    results[symbol] = rate
            except Exception as e:
                log.log_error(e, f"get_all_binance_funding - {symbol}")

        return results

    async def get_all_funding_rates(self) -> Dict[str, Dict[str, float]]:
        """
        Get funding rates from all sources.

        Returns:
        {
            "binance": {"BTC": 0.0001, "ETH": 0.0002, ...},
            "paradex": {...},
            ...
        }

        For DEXes, we use Binance as reference since most perp DEXes
        track Binance funding or have similar mechanics.
        """
        result = {}

        # Get Binance funding (reference)
        binance_rates = await self.get_all_binance_funding()
        if binance_rates:
            result["binance"] = binance_rates

        # For DEXes, we can add specific implementations later
        # For now, use Binance as proxy (most DEXes track similar rates)
        # This is a reasonable approximation for swing trading signals

        if binance_rates:
            # Map to DEX asset formats
            result["paradex"] = {k: v for k, v in binance_rates.items() if k in ["BTC", "ETH"]}
            result["hibachi"] = binance_rates.copy()
            result["nado"] = {k: v for k, v in binance_rates.items() if k in ["BTC", "ETH", "SOL"]}
            result["extended"] = {k: v for k, v in binance_rates.items() if k in ["BTC", "ETH", "SOL"]}

        self._cache = result
        self._cache_time = datetime.now(timezone.utc)

        return result

    def get_funding_signal(self, symbol: str, funding_rate: Optional[float] = None) -> Dict:
        """
        Generate a trading signal based on funding rate.

        Args:
            symbol: The asset symbol (e.g., "BTC")
            funding_rate: Optional rate to use (otherwise looks up from cache)

        Returns:
            {
                "signal": "LONG" | "SHORT" | "NEUTRAL",
                "strength": "STRONG" | "WEAK" | "NONE",
                "funding_rate": float,
                "reasoning": str
            }
        """
        if funding_rate is None:
            # Try to get from cache
            if self._cache and "binance" in self._cache:
                funding_rate = self._cache["binance"].get(symbol)

        if funding_rate is None:
            return {
                "signal": "NEUTRAL",
                "strength": "NONE",
                "funding_rate": None,
                "reasoning": f"No funding data for {symbol}"
            }

        pct = funding_rate * 100

        if funding_rate > config.FUNDING_EXTREME_POSITIVE:
            return {
                "signal": "LONG",
                "strength": "STRONG",
                "funding_rate": funding_rate,
                "reasoning": f"Extreme positive funding ({pct:+.4f}%) - shorts paying heavily, squeeze potential"
            }
        elif funding_rate > config.FUNDING_MODERATE_POSITIVE:
            return {
                "signal": "LONG",
                "strength": "WEAK",
                "funding_rate": funding_rate,
                "reasoning": f"Positive funding ({pct:+.4f}%) - slight LONG lean"
            }
        elif funding_rate < config.FUNDING_EXTREME_NEGATIVE:
            return {
                "signal": "SHORT",
                "strength": "STRONG",
                "funding_rate": funding_rate,
                "reasoning": f"Extreme negative funding ({pct:+.4f}%) - longs paying heavily, liquidation potential"
            }
        elif funding_rate < config.FUNDING_MODERATE_NEGATIVE:
            return {
                "signal": "SHORT",
                "strength": "WEAK",
                "funding_rate": funding_rate,
                "reasoning": f"Negative funding ({pct:+.4f}%) - slight SHORT lean"
            }
        else:
            return {
                "signal": "NEUTRAL",
                "strength": "NONE",
                "funding_rate": funding_rate,
                "reasoning": f"Neutral funding ({pct:+.4f}%)"
            }

    def calculate_funding_pnl(
        self,
        direction: str,
        size_usd: float,
        funding_rate: float,
        hold_hours: float
    ) -> float:
        """
        Calculate expected funding P&L over hold period.
        Funding settles every 8 hours.

        Args:
            direction: "LONG" or "SHORT"
            size_usd: Position size in USD
            funding_rate: Per-period funding rate (decimal)
            hold_hours: Expected hold time in hours

        Returns:
            Expected funding P&L in USD (positive = receive, negative = pay)
        """
        funding_periods = hold_hours / 8.0
        per_period = size_usd * abs(funding_rate)

        if direction == "LONG":
            # LONG pays when funding positive, receives when negative
            return -per_period * funding_periods if funding_rate > 0 else per_period * funding_periods
        else:
            # SHORT receives when funding positive, pays when negative
            return per_period * funding_periods if funding_rate > 0 else -per_period * funding_periods

    def get_best_direction_for_funding(self, symbol: str) -> Tuple[str, str]:
        """
        Get the best direction based on funding.

        Returns:
            (direction, strength) tuple
        """
        signal = self.get_funding_signal(symbol)
        return signal["signal"], signal["strength"]


# Global instance
_monitor: Optional[FundingMonitor] = None


def get_monitor() -> FundingMonitor:
    """Get or create the global funding monitor."""
    global _monitor
    if _monitor is None:
        _monitor = FundingMonitor()
    return _monitor


async def fetch_funding_rates() -> Dict[str, Dict[str, float]]:
    """Convenience function to fetch all funding rates."""
    monitor = get_monitor()
    return await monitor.get_all_funding_rates()


async def get_funding_signal(symbol: str) -> Dict:
    """Convenience function to get funding signal for a symbol."""
    monitor = get_monitor()
    # Ensure we have fresh data
    await monitor.get_all_funding_rates()
    return monitor.get_funding_signal(symbol)
