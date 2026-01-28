"""
Binance Futures Data Proxy for Nado
Uses Binance Futures API to fetch klines and funding rates as proxy data for Nado trading.

Since Nado doesn't provide historical candles via API, we use Binance as a proxy
since both exchanges trade the same underlying assets (BTC, ETH, SOL, etc.).
"""

import logging
import aiohttp
import pandas as pd
from typing import Optional, Dict, List
from datetime import datetime

logger = logging.getLogger(__name__)


class BinanceFuturesProxy:
    """
    Fetch market data from Binance Futures as proxy for Nado DEX

    Binance provides:
    - Klines (OHLCV candles) for technical indicators (RSI, MACD, etc.)
    - Funding rates for sentiment/positioning data
    - 24h ticker data for volume and price change
    """

    BINANCE_FUTURES_URL = "https://fapi.binance.com"

    # Mapping: Nado symbol → Binance Futures symbol
    SYMBOL_MAP = {
        'BTC': 'BTCUSDT',
        'ETH': 'ETHUSDT',
        'SOL': 'SOLUSDT',
        'XRP': 'XRPUSDT',
        'BNB': 'BNBUSDT',
        'HYPE': 'HYPEUSDT',
        'ZEC': 'ZECUSDT',
        'SUI': 'SUIUSDT',
        'AAVE': 'AAVEUSDT',
        'TAO': 'TAOUSDT',
        'LIT': 'LITUSDT',
        'kPEPE': 'PEPEUSDT',  # kPEPE maps to PEPE on Binance
        'PENGU': 'PENGUUSDT',
        'XMR': 'XMRUSDT',  # Note: Binance delisted XMR futures
        # These likely don't have Binance futures:
        # FARTCOIN, MON, PUMP, XAUT (gold), USELESS
    }

    def __init__(self):
        """Initialize Binance Futures proxy"""
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """Close the session"""
        if self._session and not self._session.closed:
            await self._session.close()

    def nado_to_binance(self, nado_symbol: str) -> Optional[str]:
        """
        Convert Nado symbol to Binance Futures symbol

        Args:
            nado_symbol: e.g., 'BTC' or 'BTC-PERP'

        Returns:
            Binance symbol e.g., 'BTCUSDT' or None if not mapped
        """
        # Strip -PERP suffix if present
        base = nado_symbol.replace('-PERP', '')
        return self.SYMBOL_MAP.get(base)

    async def fetch_klines(
        self,
        nado_symbol: str,
        interval: str = "15m",
        limit: int = 100
    ) -> Optional[pd.DataFrame]:
        """
        Fetch kline (OHLCV) data from Binance Futures

        Args:
            nado_symbol: Nado symbol (e.g., 'SOL' or 'SOL-PERP')
            interval: Candle interval (1m, 5m, 15m, 1h, 4h, 1d)
            limit: Number of candles (max 1500)

        Returns:
            DataFrame with columns: timestamp, open, high, low, close, volume
        """
        binance_symbol = self.nado_to_binance(nado_symbol)
        if not binance_symbol:
            logger.debug(f"No Binance mapping for Nado symbol: {nado_symbol}")
            return None

        try:
            session = await self._get_session()
            url = f"{self.BINANCE_FUTURES_URL}/fapi/v1/klines"
            params = {
                'symbol': binance_symbol,
                'interval': interval,
                'limit': min(limit, 1500)
            }

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.debug(f"Binance klines error for {binance_symbol}: {error}")
                    return None

                data = await resp.json()
                if not data:
                    return None

                # Parse Binance kline format
                df = pd.DataFrame(data, columns=[
                    'timestamp', 'open', 'high', 'low', 'close', 'volume',
                    'close_time', 'quote_volume', 'trades', 'taker_buy_base',
                    'taker_buy_quote', 'ignore'
                ])

                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df['open'] = pd.to_numeric(df['open'])
                df['high'] = pd.to_numeric(df['high'])
                df['low'] = pd.to_numeric(df['low'])
                df['close'] = pd.to_numeric(df['close'])
                df['volume'] = pd.to_numeric(df['volume'])

                df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
                return df

        except Exception as e:
            logger.debug(f"Error fetching Binance klines for {nado_symbol}: {e}")
            return None

    async def fetch_funding_rate(self, nado_symbol: str) -> Optional[Dict]:
        """
        Fetch current funding rate from Binance Futures

        Args:
            nado_symbol: Nado symbol (e.g., 'BTC')

        Returns:
            Dict with funding_rate, funding_rate_pct, mark_price, annualized_rate
        """
        binance_symbol = self.nado_to_binance(nado_symbol)
        if not binance_symbol:
            return None

        try:
            session = await self._get_session()
            url = f"{self.BINANCE_FUTURES_URL}/fapi/v1/premiumIndex"
            params = {'symbol': binance_symbol}

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                funding_rate = float(data.get('lastFundingRate', 0))
                # Annualized = funding_rate * 3 * 365 (3 funding periods per day)
                annualized = funding_rate * 3 * 365 * 100

                return {
                    'funding_rate': funding_rate,
                    'funding_rate_pct': f"{funding_rate * 100:+.4f}%",
                    'annualized_rate': annualized,
                    'annualized_pct': f"{annualized:+.2f}%",
                    'mark_price': float(data.get('markPrice', 0)),
                }

        except Exception as e:
            logger.debug(f"Error fetching Binance funding for {nado_symbol}: {e}")
            return None

    async def fetch_24h_ticker(self, nado_symbol: str) -> Optional[Dict]:
        """
        Fetch 24h ticker data from Binance Futures

        Args:
            nado_symbol: Nado symbol (e.g., 'BTC')

        Returns:
            Dict with price_change_pct, volume_24h, high_24h, low_24h
        """
        binance_symbol = self.nado_to_binance(nado_symbol)
        if not binance_symbol:
            return None

        try:
            session = await self._get_session()
            url = f"{self.BINANCE_FUTURES_URL}/fapi/v1/ticker/24hr"
            params = {'symbol': binance_symbol}

            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None

                data = await resp.json()
                return {
                    'price_change_pct': float(data.get('priceChangePercent', 0)),
                    'volume_24h': float(data.get('volume', 0)),
                    'quote_volume_24h': float(data.get('quoteVolume', 0)),
                    'high_24h': float(data.get('highPrice', 0)),
                    'low_24h': float(data.get('lowPrice', 0)),
                    'last_price': float(data.get('lastPrice', 0)),
                }

        except Exception as e:
            logger.debug(f"Error fetching Binance 24h ticker for {nado_symbol}: {e}")
            return None

    def calculate_indicators(self, df: pd.DataFrame) -> Dict:
        """
        Calculate technical indicators from kline data

        Args:
            df: DataFrame with OHLCV data

        Returns:
            Dict with RSI, MACD, SMA, volume analysis
        """
        if df is None or len(df) < 26:
            return {}

        try:
            # RSI (14 period)
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss.replace(0, 0.0001)
            rsi = 100 - (100 / (1 + rs))
            current_rsi = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50

            # MACD
            ema_12 = df['close'].ewm(span=12, adjust=False).mean()
            ema_26 = df['close'].ewm(span=26, adjust=False).mean()
            macd = ema_12 - ema_26
            signal = macd.ewm(span=9, adjust=False).mean()
            macd_histogram = float(macd.iloc[-1] - signal.iloc[-1])
            macd_value = float(macd.iloc[-1])
            macd_signal = float(signal.iloc[-1])

            # SMAs
            sma_10 = df['close'].rolling(window=10).mean().iloc[-1]
            sma_20 = df['close'].rolling(window=20).mean().iloc[-1]

            # Volume analysis
            avg_volume = df['volume'].mean()
            recent_volume = df['volume'].iloc[-5:].mean()
            volume_ratio = recent_volume / avg_volume if avg_volume > 0 else 1

            # Price momentum
            price_start = df['close'].iloc[0]
            price_end = df['close'].iloc[-1]
            momentum_pct = ((price_end - price_start) / price_start * 100) if price_start > 0 else 0

            return {
                'rsi': current_rsi,
                'macd': macd_value,
                'macd_signal': macd_signal,
                'macd_histogram': macd_histogram,
                'sma_10': float(sma_10) if not pd.isna(sma_10) else 0,
                'sma_20': float(sma_20) if not pd.isna(sma_20) else 0,
                'volume_ratio': volume_ratio,
                'momentum_pct': momentum_pct,
            }

        except Exception as e:
            logger.error(f"Error calculating indicators: {e}")
            return {}

    async def fetch_full_market_data(self, nado_symbol: str) -> Optional[Dict]:
        """
        Fetch comprehensive market data for a Nado symbol

        Args:
            nado_symbol: Nado symbol (e.g., 'BTC')

        Returns:
            Dict with indicators, funding, 24h stats
        """
        # Fetch all data in parallel
        klines = await self.fetch_klines(nado_symbol, interval='15m', limit=100)
        funding = await self.fetch_funding_rate(nado_symbol)
        ticker = await self.fetch_24h_ticker(nado_symbol)

        # Calculate indicators
        indicators = self.calculate_indicators(klines) if klines is not None else {}

        return {
            'symbol': nado_symbol,
            'has_binance_data': klines is not None,
            # Indicators
            'rsi': indicators.get('rsi'),
            'macd': indicators.get('macd'),
            'macd_histogram': indicators.get('macd_histogram'),
            'sma_10': indicators.get('sma_10'),
            'sma_20': indicators.get('sma_20'),
            'volume_ratio': indicators.get('volume_ratio'),
            # Funding
            'funding_rate': funding.get('funding_rate') if funding else None,
            'funding_rate_pct': funding.get('funding_rate_pct') if funding else None,
            'annualized_funding': funding.get('annualized_rate') if funding else None,
            'annualized_funding_pct': funding.get('annualized_pct') if funding else None,
            # 24h stats
            'price_change_24h': ticker.get('price_change_pct') if ticker else None,
            'volume_24h': ticker.get('quote_volume_24h') if ticker else None,
        }


# Test function
async def test_binance_proxy():
    """Test Binance proxy functionality for Nado"""
    proxy = BinanceFuturesProxy()

    try:
        print("Testing Binance Futures Proxy for Nado...")
        print("=" * 60)

        symbols = ['BTC', 'ETH', 'SOL', 'XMR', 'FARTCOIN']

        for symbol in symbols:
            print(f"\n📊 {symbol}:")
            data = await proxy.fetch_full_market_data(symbol)

            if data.get('has_binance_data'):
                print(f"   RSI: {data.get('rsi', 'N/A'):.1f}")
                print(f"   MACD Hist: {data.get('macd_histogram', 'N/A'):.4f}")
                print(f"   Funding (ann): {data.get('annualized_funding_pct', 'N/A')}")
                print(f"   24h Change: {data.get('price_change_24h', 'N/A'):.2f}%")
                print(f"   Volume 24h: ${data.get('volume_24h', 0):,.0f}")
            else:
                print(f"   ❌ No Binance data available")

    finally:
        await proxy.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(test_binance_proxy())
