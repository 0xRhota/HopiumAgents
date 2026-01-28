"""
Nado DEX Data Fetcher
Fetches market data, orderbook, and positions from Nado

Nado is a perp DEX on Ink (L2) with:
- Up to 20x leverage on BTC, ETH, SOL, BNB, XRP
- USDT0 as settlement currency
- Zero maker fees (rebates!), low taker fees

Uses Binance Futures as proxy for:
- Technical indicators (RSI, MACD, SMA)
- Funding rates
- 24h volume and price change
"""

import asyncio
import logging
import pandas as pd
import os
from typing import Optional, Dict, List
from datetime import datetime, timedelta
import time
from dotenv import load_dotenv

from nado_agent.data.binance_proxy import BinanceFuturesProxy

load_dotenv()

logger = logging.getLogger(__name__)


class NadoDataFetcher:
    """
    Fetch market data from Nado DEX
    Uses NadoSDK for API interactions
    """

    def __init__(self, nado_sdk=None):
        """
        Initialize Nado data fetcher

        Args:
            nado_sdk: NadoSDK instance (initialized externally)
        """
        self.sdk = nado_sdk
        self.available_markets = []
        self.market_info = {}  # Symbol -> market metadata
        self._initialized = False
        # Binance proxy for indicators, funding, volume (Nado doesn't provide these via API)
        self.binance_proxy = BinanceFuturesProxy()

    async def initialize(self):
        """Initialize markets list from Nado API"""
        if self._initialized or not self.sdk:
            return

        try:
            products = await self.sdk.get_products()
            if products:
                for product in products:
                    symbol = product.get('symbol', '')
                    # Nado uses format like "BTC-PERP", "ETH-PERP"
                    if symbol.endswith('-PERP'):
                        base = symbol.replace('-PERP', '')
                        self.available_markets.append(base)
                        self.market_info[base] = {
                            'symbol': symbol,
                            'product_id': product.get('product_id', product.get('id')),
                            'base_currency': base,
                            'quote_currency': 'USDT0',
                            'tick_size': float(product.get('tick_size', 0.01)),
                            'step_size': float(product.get('step_size', 0.001)),
                            'min_size': float(product.get('min_size', 0.001)),
                            'max_leverage': float(product.get('max_leverage', 20)),
                        }

            self._initialized = True
            logger.info(f"Initialized Nado with {len(self.available_markets)} markets")

        except Exception as e:
            logger.error(f"Failed to initialize Nado markets: {e}")

    def _get_full_symbol(self, symbol: str) -> str:
        """Convert base symbol to full Nado symbol"""
        if symbol.endswith('-PERP'):
            return symbol
        return f"{symbol}-PERP"

    def _get_product_id(self, symbol: str) -> Optional[int]:
        """Get product ID for a symbol"""
        base = symbol.replace('-PERP', '')
        info = self.market_info.get(base, {})
        return info.get('product_id')

    async def fetch_bbo(self, symbol: str) -> Optional[Dict]:
        """
        Fetch best bid/offer for a symbol

        Args:
            symbol: Base symbol (e.g., "ETH") or full symbol (e.g., "ETH-PERP")

        Returns:
            Dict with bid, ask, spread info
        """
        if not self.sdk:
            return None

        base = symbol.replace('-PERP', '')
        full_symbol = self._get_full_symbol(symbol)
        product_id = self._get_product_id(symbol)

        if not product_id:
            logger.debug(f"No product_id for {symbol}")
            return None

        try:
            # Nado market_price returns bid_x18 and ask_x18
            response = await self.sdk._query("market_price", {"product_id": str(product_id)})

            if response.get("status") == "success":
                data = response.get("data", {})

                # Parse x18 format prices
                bid_x18 = data.get('bid_x18', '0')
                ask_x18 = data.get('ask_x18', '0')

                bid = self.sdk._from_x18(int(bid_x18)) if bid_x18 else 0
                ask = self.sdk._from_x18(int(ask_x18)) if ask_x18 else 0

                if bid > 0 and ask > 0:
                    spread = ask - bid
                    spread_pct = (spread / bid * 100)

                    return {
                        'symbol': base,
                        'bid': bid,
                        'ask': ask,
                        'spread': spread,
                        'spread_pct': spread_pct,
                        'mid_price': (bid + ask) / 2
                    }

            return None

        except Exception as e:
            logger.debug(f"BBO fetch error for {symbol}: {e}")
            return None

    async def fetch_orderbook(self, symbol: str, depth: int = 10) -> Optional[Dict]:
        """
        Fetch orderbook for a symbol
        NOTE: Nado doesn't expose orderbook via API currently, so we return None
        """
        # Nado API doesn't provide orderbook data via query endpoint
        # The market_price endpoint only gives best bid/ask
        return None

    async def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """
        Fetch current funding rate for a symbol
        NOTE: Nado doesn't expose funding rate via API currently
        """
        # Nado API doesn't provide funding_rate query endpoint
        # Would need to check if it's embedded in product info
        return None

    def calculate_technicals(self, prices: List[float], volumes: Optional[List[float]] = None) -> Dict:
        """
        Calculate technical indicators from price data

        Args:
            prices: List of recent prices
            volumes: List of volumes (optional)

        Returns:
            Dict with RSI, MACD, volume analysis
        """
        if not prices or len(prices) < 20:
            return {}

        try:
            df = pd.DataFrame({'price': prices})
            if volumes:
                df['volume'] = volumes

            # RSI (14 period)
            delta = df['price'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss.replace(0, 0.0001)
            rsi = 100 - (100 / (1 + rs))
            current_rsi = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50

            # Simple moving averages
            sma_fast = df['price'].rolling(window=10).mean().iloc[-1]
            sma_slow = df['price'].rolling(window=20).mean().iloc[-1]

            # MACD (simplified)
            ema_12 = df['price'].ewm(span=12, adjust=False).mean()
            ema_26 = df['price'].ewm(span=26, adjust=False).mean()
            macd = ema_12 - ema_26
            signal = macd.ewm(span=9, adjust=False).mean()
            macd_histogram = float(macd.iloc[-1] - signal.iloc[-1])

            # Volume analysis
            volume_ratio = 1
            if volumes and 'volume' in df.columns:
                avg_volume = df['volume'].mean()
                recent_volume = df['volume'].iloc[-5:].mean()
                volume_ratio = recent_volume / avg_volume if avg_volume > 0 else 1

            # Price momentum
            price_change_pct = ((prices[-1] - prices[0]) / prices[0] * 100) if prices[0] > 0 else 0

            return {
                'rsi': current_rsi,
                'sma_fast': float(sma_fast) if not pd.isna(sma_fast) else 0,
                'sma_slow': float(sma_slow) if not pd.isna(sma_slow) else 0,
                'macd_histogram': macd_histogram,
                'volume_ratio': volume_ratio,
                'price_change_pct': price_change_pct,
                'current_price': prices[-1],
                'high_price': max(prices),
                'low_price': min(prices)
            }

        except Exception as e:
            logger.debug(f"Technical calculation error: {e}")
            return {}

    async def fetch_market_data(self, symbol: str, include_binance: bool = True) -> Optional[Dict]:
        """
        Fetch comprehensive market data for a symbol

        Args:
            symbol: Base symbol (e.g., "ETH")
            include_binance: Whether to fetch indicators from Binance (default True)

        Returns:
            Dict with all market data including Binance indicators
        """
        bbo = await self.fetch_bbo(symbol)
        if not bbo:
            return None

        base = symbol.replace('-PERP', '')
        market_info = self.market_info.get(base, {})

        result = {
            'symbol': base,
            'full_symbol': self._get_full_symbol(symbol),
            'product_id': market_info.get('product_id'),
            'price': bbo.get('mid_price', 0),
            'bid': bbo.get('bid', 0),
            'ask': bbo.get('ask', 0),
            'spread_pct': bbo.get('spread_pct', 0),
            'min_order_size': market_info.get('min_size', 0),
            'step_size': market_info.get('step_size', 0),
            'max_leverage': market_info.get('max_leverage', 20),
            'timestamp': datetime.now().isoformat(),
            # Binance indicators (will be populated below if available)
            'rsi': None,
            'macd_histogram': None,
            'sma_10': None,
            'sma_20': None,
            'volume_ratio': None,
            'funding_rate': None,
            'funding_rate_pct': None,
            'annualized_funding': None,
            'price_change_24h': None,
            'volume_24h': None,
            'has_indicators': False,
        }

        # Fetch Binance indicators if requested
        if include_binance:
            binance_data = await self.binance_proxy.fetch_full_market_data(base)
            if binance_data and binance_data.get('has_binance_data'):
                result['rsi'] = binance_data.get('rsi')
                result['macd_histogram'] = binance_data.get('macd_histogram')
                result['sma_10'] = binance_data.get('sma_10')
                result['sma_20'] = binance_data.get('sma_20')
                result['volume_ratio'] = binance_data.get('volume_ratio')
                result['funding_rate'] = binance_data.get('funding_rate')
                result['funding_rate_pct'] = binance_data.get('funding_rate_pct')
                result['annualized_funding'] = binance_data.get('annualized_funding')
                result['price_change_24h'] = binance_data.get('price_change_24h')
                result['volume_24h'] = binance_data.get('volume_24h')
                result['has_indicators'] = True

        return result

    async def fetch_all_markets(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict]:
        """
        Fetch market data for all available Nado markets

        Args:
            symbols: List of symbols to fetch (default: all available)

        Returns:
            Dict mapping symbol -> market data
        """
        await self.initialize()

        if symbols is None:
            symbols = self.available_markets

        results = {}
        for i, symbol in enumerate(symbols):
            data = await self.fetch_market_data(symbol)
            if data:
                results[symbol] = data

            # Rate limiting
            if i < len(symbols) - 1:
                await asyncio.sleep(0.1)  # 100ms delay

        logger.info(f"Fetched data for {len(results)}/{len(symbols)} Nado markets")
        return results

    async def fetch_positions(self) -> List[Dict]:
        """
        Fetch open positions

        Returns:
            List of position dicts
        """
        if not self.sdk:
            return []

        try:
            positions = await self.sdk.get_positions()
            result = []

            for pos in positions:
                amount = pos.get('amount', pos.get('size', 0))
                if isinstance(amount, str):
                    amount = self.sdk._from_x18(int(amount))
                else:
                    amount = float(amount)

                if amount != 0:
                    symbol = pos.get('symbol', pos.get('product_symbol', '')).replace('-PERP', '')
                    entry_price = pos.get('entry_price', pos.get('average_entry_price', 0))
                    if isinstance(entry_price, str):
                        entry_price = self.sdk._from_x18(int(entry_price))
                    else:
                        entry_price = float(entry_price)

                    unrealized_pnl = pos.get('unrealized_pnl', 0)
                    if isinstance(unrealized_pnl, str):
                        unrealized_pnl = self.sdk._from_x18(int(unrealized_pnl))
                    else:
                        unrealized_pnl = float(unrealized_pnl)

                    result.append({
                        'symbol': symbol,
                        'full_symbol': self._get_full_symbol(symbol),
                        'side': 'LONG' if amount > 0 else 'SHORT',
                        'size': abs(amount),
                        'entry_price': entry_price,
                        'unrealized_pnl': unrealized_pnl,
                    })

            return result

        except Exception as e:
            logger.error(f"Positions fetch error: {e}")
            return []

    async def fetch_account_summary(self) -> Dict:
        """
        Fetch account summary

        Returns:
            Dict with account balance and margin info
        """
        if not self.sdk:
            return {}

        try:
            balance = await self.sdk.get_balance()
            info = await self.sdk.get_subaccount_info()

            return {
                'account_value': balance or 0,
                'free_collateral': info.get('free_collateral', balance) if info else balance,
                'unrealized_pnl': info.get('unrealized_pnl', 0) if info else 0
            }

        except Exception as e:
            logger.error(f"Account summary fetch error: {e}")
            return {}

    def get_tradeable_symbols(self) -> List[str]:
        """Get list of tradeable symbols"""
        return self.available_markets.copy()

    async def close(self):
        """Close resources"""
        if self.binance_proxy:
            await self.binance_proxy.close()
