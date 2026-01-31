"""
Extended Market Data Aggregator
Uses Extended DEX data (Starknet perpetuals)

Symbols fetched dynamically from Extended API via SDK
"""

import logging
from typing import Dict, Optional, List
from datetime import datetime, timedelta

# Import shared components
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from llm_agent.data.macro_fetcher import MacroContextFetcher
from llm_agent.data.indicator_calculator import IndicatorCalculator
from extended_agent.data.extended_fetcher import ExtendedDataFetcher

logger = logging.getLogger(__name__)

# WHITELIST: Only trade selected symbols
# Extended has 72 markets - majors + XCU (new listing, points boost)
WHITELISTED_SYMBOLS = {"BTC-USD", "ETH-USD", "SOL-USD", "XCU-USD"}


class ExtendedMarketDataAggregator:
    """
    Market data aggregator for Extended DEX (Starknet)
    Uses Extended API for OHLCV data
    Symbols fetched dynamically from API
    """

    def __init__(
        self,
        cambrian_api_key: str,
        sdk=None,
        interval: str = "5m",  # 5m candles for HF scalping
        candle_limit: int = 100,
        macro_refresh_hours: int = 12
    ):
        """
        Initialize Extended market data aggregator

        Args:
            cambrian_api_key: Cambrian API key for Deep42 (macro context only)
            sdk: ExtendedSDK instance for fetching market data
            interval: Candle interval (default: 5m for HF scalping)
            candle_limit: Number of candles to fetch (default: 100)
            macro_refresh_hours: Hours between macro context refreshes (default: 12)
        """
        self.interval = interval
        self.candle_limit = candle_limit

        # Extended data fetcher with SDK
        self.extended = ExtendedDataFetcher(sdk=sdk)

        # Shared components (macro context, indicators)
        self.macro_fetcher = MacroContextFetcher(
            cambrian_api_key=cambrian_api_key,
            refresh_interval_hours=macro_refresh_hours
        )
        self.indicator_calc = IndicatorCalculator()

        logger.info(f"✅ ExtendedMarketDataAggregator initialized (interval={interval}, candles={candle_limit})")

    @property
    def extended_markets(self) -> List[str]:
        """
        Get list of available Extended markets from fetcher
        WHITELIST: Only selected symbols

        Returns:
            List of symbol strings
        """
        all_symbols = self.extended.available_symbols
        # WHITELIST mode: only trade specific symbols
        filtered = [s for s in all_symbols if s in WHITELISTED_SYMBOLS]
        logger.info(f"Whitelist active: trading {len(filtered)} symbols: {', '.join(sorted(filtered))}")
        return filtered

    async def fetch_market_data(self, symbol: str) -> Optional[Dict]:
        """
        Fetch comprehensive market data for a single Extended symbol (async)

        Args:
            symbol: Extended symbol (e.g., "BTC-USD")

        Returns:
            Dict with all market data, or None if symbol not available
        """
        # Initialize symbols if needed
        await self.extended._initialize_symbols()

        # Check if symbol is available on Extended
        if symbol not in self.extended.available_symbols:
            logger.warning(f"Symbol {symbol} not available on Extended (available: {len(self.extended.available_symbols)} markets)")
            return None

        # Fetch Extended data
        extended_data = await self.extended.fetch_market_data(
            symbol=symbol,
            interval=self.interval,
            limit=self.candle_limit
        )

        if not extended_data:
            logger.warning(f"No data returned from Extended for {symbol}")
            return None

        kline_df = extended_data['kline_df']
        funding_rate = extended_data['funding_rate']
        current_price = extended_data['current_price']
        oi = extended_data.get('oi')
        volume_24h = extended_data.get('volume_24h')

        # Calculate indicators if we have kline data
        if kline_df is not None and not kline_df.empty:
            kline_df = self.indicator_calc.calculate_all_indicators(kline_df)
            indicators = self.indicator_calc.get_latest_values(kline_df)
            # Use calculated volume if API volume not available
            if not volume_24h:
                volume_24h = self._calculate_24h_volume(kline_df)
        else:
            # No kline data - use current price only
            indicators = {'price': current_price}

        return {
            "symbol": symbol,
            "price": current_price or indicators.get('price'),
            "volume_24h": volume_24h,
            "funding_rate": funding_rate,
            "oi": oi,
            "indicators": indicators,
            "kline_df": kline_df
        }

    async def fetch_all_markets(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict]:
        """
        Fetch market data for all Extended markets (async)

        Args:
            symbols: List of symbols (default: whitelisted markets)

        Returns:
            Dict mapping symbol -> market data dict
        """
        # Initialize symbols if not already done
        await self.extended._initialize_symbols()

        if symbols is None:
            symbols = self.extended_markets

            if not symbols:
                # If whitelist is empty, fallback to top markets
                logger.warning("Whitelist returned empty - falling back to BTC-USD, ETH-USD, SOL-USD")
                symbols = ["BTC-USD", "ETH-USD", "SOL-USD"]

            logger.info(f"🔍 Using {len(symbols)} Extended markets")
        else:
            # Filter to only available Extended markets
            available = set(self.extended.available_symbols)
            symbols = [s for s in symbols if s in available]
            if not symbols:
                logger.warning(f"No valid Extended symbols in provided list")
                return {}

        # Use fetcher's async method
        results = await self.extended.fetch_all_markets(
            symbols=symbols,
            interval=self.interval,
            limit=self.candle_limit
        )

        # Process each result to add indicators
        for symbol, data in results.items():
            kline_df = data.get('kline_df')
            if kline_df is not None and not kline_df.empty:
                kline_df = self.indicator_calc.calculate_all_indicators(kline_df)
                indicators = self.indicator_calc.get_latest_values(kline_df)
                volume_24h = data.get('volume_24h') or self._calculate_24h_volume(kline_df)
            else:
                # No kline data - use current price only
                indicators = {'price': data.get('current_price')}
                volume_24h = data.get('volume_24h')

            data['indicators'] = indicators
            data['volume_24h'] = volume_24h
            data['price'] = data.get('current_price') or indicators.get('price')
            data['kline_df'] = kline_df

        logger.info(f"✅ Fetched data for {len(results)}/{len(symbols)} Extended markets")
        return results

    def get_macro_context(self, force_refresh: bool = False) -> str:
        """Get macro market context"""
        return self.macro_fetcher.get_macro_context(force_refresh=force_refresh)

    def format_market_table(self, market_data: Dict[str, Dict]) -> str:
        """
        Format market data as table

        Args:
            market_data: Dict from fetch_all_markets()

        Returns:
            Formatted table string
        """
        lines = []
        lines.append("Market Data (Latest):")
        lines.append("Sources: Extended DEX (Price, OI, Funding), Calculated (Indicators)")
        lines.append(
            f"{'Symbol':<15} {'Price':>12} {'24h Vol':>12} "
            f"{'Funding':>10} {'OI':>15} {'RSI':>6} {'MACD':>8} {'SMA20>50':>10}"
        )
        lines.append("-" * 100)

        for symbol, data in market_data.items():
            if data is None:
                lines.append(f"{symbol:<15} {'N/A':>12}")
                continue

            price = data.get('price', 0)
            volume = data.get('volume_24h', 0)
            funding = data.get('funding_rate', 0)
            oi = data.get('oi')
            indicators = data.get('indicators', {})

            rsi = indicators.get('rsi', 0)
            macd_diff = indicators.get('macd_diff', 0)
            sma_20_above_50 = indicators.get('sma_20_above_50', False)

            price_str = f"${price:,.2f}" if price else "N/A"
            volume_str = f"${volume/1000:.0f}K" if volume else "N/A"
            funding_str = f"{funding*100:.4f}%" if funding else "N/A"
            oi_str = f"${oi:,.0f}" if oi else "N/A"
            rsi_str = f"{rsi:.0f}" if rsi else "N/A"
            macd_str = f"{macd_diff:+.1f}" if macd_diff is not None else "N/A"
            sma_str = "Yes" if sma_20_above_50 else "No"

            lines.append(
                f"{symbol:<15} {price_str:>12} {volume_str:>12} "
                f"{funding_str:>10} {oi_str:>15} {rsi_str:>6} {macd_str:>8} {sma_str:>10}"
            )

        return "\n".join(lines)

    def _calculate_24h_volume(self, kline_df) -> Optional[float]:
        """Calculate 24h volume from candles"""
        if kline_df is None or kline_df.empty:
            return None

        # Sum volume from last 24h of candles
        if 'timestamp' in kline_df.columns and 'volume' in kline_df.columns:
            now = datetime.now()
            cutoff = now - timedelta(hours=24)

            # Filter candles within last 24h
            recent_candles = kline_df[kline_df['timestamp'] >= cutoff]
            if not recent_candles.empty:
                volume_sum = recent_candles['volume'].sum()
                # Get average price for USD conversion
                avg_price = recent_candles['close'].mean()
                if avg_price > 0:
                    return float(volume_sum * avg_price)

            # Fallback: use last 96 candles (24h of 15m candles)
            last_96 = kline_df.tail(96)
            if not last_96.empty and 'close' in last_96.columns:
                volume_sum = last_96['volume'].sum()
                avg_price = last_96['close'].mean()
                if avg_price > 0:
                    return float(volume_sum * avg_price)

        return None


# Test the aggregator
if __name__ == "__main__":
    import asyncio
    import os

    async def test():
        cambrian_key = os.getenv("CAMBRIAN_API_KEY")
        if not cambrian_key:
            raise ValueError("CAMBRIAN_API_KEY environment variable not set")

        aggregator = ExtendedMarketDataAggregator(
            cambrian_api_key=cambrian_key,
            interval="5m",
            candle_limit=50
        )

        # Fetch all whitelisted markets
        market_data = await aggregator.fetch_all_markets()

        if market_data:
            print(aggregator.format_market_table(market_data))
        else:
            print("No market data fetched")

    asyncio.run(test())
