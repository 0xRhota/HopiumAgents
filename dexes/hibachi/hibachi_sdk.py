"""
Hibachi DEX SDK Wrapper
REST API implementation with HMAC authentication
"""

import asyncio
import aiohttp
import hmac
import hashlib
import time
import json
import os
import logging
import base64
import struct
from typing import Dict, Optional, List
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class HibachiSDK:
    """REST API wrapper for Hibachi trading"""

    def __init__(self, api_key: str, api_secret: str, account_id: Optional[str] = None):
        self.base_url = "https://api.hibachi.xyz"
        self.data_api_url = "https://data-api.hibachi.xyz"
        self.api_key = api_key
        # Per official Hibachi TypeScript SDK (sdk_hmac.ts), use raw string bytes
        # Buffer.from('your-private-key') in Node.js = UTF-8 encoding by default
        self.api_secret_bytes = api_secret.encode('utf-8')
        self._account_id = account_id  # Account ID from Hibachi UI (Settings → API Keys)
        logger.debug(f"SDK initialized with secret length: {len(self.api_secret_bytes)} bytes")

    def _get_headers(self) -> Dict[str, str]:
        """
        Generate headers for Hibachi API

        Returns:
            Dictionary of headers with API key
        """
        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json"
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
        use_data_api: bool = False
    ) -> Dict:
        """
        Make authenticated API request

        Args:
            method: HTTP method (GET, POST, DELETE)
            endpoint: API endpoint path
            params: Query parameters
            data: Request body data
            use_data_api: Use data API base URL instead of main API

        Returns:
            JSON response
        """
        base = self.data_api_url if use_data_api else self.base_url
        url = f"{base}{endpoint}"
        headers = self._get_headers()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=data if data else None
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        error_text = await resp.text()
                        logger.error(f"API Error {resp.status}: {error_text}")
                        return {"error": error_text, "status": resp.status}

        except Exception as e:
            logger.error(f"Request failed: {e}")
            return {"error": str(e)}

    def get_account_id(self) -> Optional[str]:
        """
        Get stored account ID

        Returns:
            Account ID string or None if not set
        """
        return self._account_id

    def set_account_id(self, account_id: str):
        """
        Set the account ID

        Args:
            account_id: Account ID from Hibachi UI (Settings → API Keys)
        """
        self._account_id = account_id
        logger.info(f"Account ID set: {account_id}")

    async def get_markets(self) -> List[Dict]:
        """
        Get all available markets

        Returns:
            List of market information (futureContracts)
        """
        try:
            response = await self._request("GET", "/market/exchange-info", use_data_api=True)
            if "error" in response:
                logger.error(f"Failed to get markets: {response['error']}")
                return []

            # Markets are under "futureContracts" key
            markets = response.get("futureContracts", [])
            return markets
        except Exception as e:
            logger.error(f"Error getting markets: {e}")
            return []

    async def get_balance(self) -> Optional[float]:
        """
        Get account balance

        Returns:
            Available balance in USDT
        """
        try:
            # Get account ID
            account_id = self.get_account_id()
            if not account_id:
                logger.error("Cannot get balance: account ID not set. Use set_account_id() or pass to constructor.")
                return None

            response = await self._request("GET", f"/capital/balance", params={"accountId": account_id})
            if "error" in response:
                logger.error(f"Failed to get balance: {response['error']}")
                return None

            # Parse balance from response (exact field TBD)
            balance = response.get("balance") or response.get("available_balance") or response.get("availableBalance")
            if balance is not None:
                return float(balance)

            logger.warning(f"Balance not found in response: {response}")
            return None

        except Exception as e:
            logger.error(f"Error getting balance: {e}")
            return None

    async def get_account_info(self) -> Optional[Dict]:
        """
        Get full account info including margin details

        Returns:
            Dict with balance, equity, available_margin, etc.
        """
        try:
            account_id = self.get_account_id()
            if not account_id:
                logger.error("Cannot get account info: account ID not set.")
                return None

            response = await self._request("GET", f"/capital/balance", params={"accountId": account_id})
            if "error" in response:
                logger.error(f"Failed to get account info: {response['error']}")
                return None

            # Return full response for caller to parse
            return response

        except Exception as e:
            logger.error(f"Error getting account info: {e}")
            return None

    async def get_positions(self) -> List[Dict]:
        """
        Get all open positions

        Returns:
            List of position dictionaries
        """
        try:
            # Get account ID
            account_id = self.get_account_id()
            if not account_id:
                logger.error("Cannot get positions: account ID not set. Use set_account_id() or pass to constructor.")
                return []

            response = await self._request("GET", "/trade/account/info", params={"accountId": account_id})
            if "error" in response:
                logger.error(f"Failed to get positions: {response['error']}")
                return []

            # Parse positions from response (may be under "positions" or "openPositions" key)
            positions = response.get("positions", response.get("openPositions", []))
            return positions

        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

    async def get_position_size(self, symbol: str) -> float:
        """
        Get position size for a specific symbol

        Args:
            symbol: Market symbol (e.g., "BTC/USDT-P")

        Returns:
            Position size (positive for long, negative for short, 0 if no position)
        """
        try:
            positions = await self.get_positions()
            for p in positions:
                if p.get('symbol') == symbol:
                    # API returns 'quantity' not 'size', and 'direction' for Long/Short
                    qty = float(p.get('quantity', p.get('size', 0)))
                    direction = p.get('direction', 'Long')
                    # Return negative for short positions
                    if direction == 'Short':
                        qty = -qty
                    return qty
            return 0.0
        except Exception as e:
            logger.error(f"Error getting position size for {symbol}: {e}")
            return 0.0

    async def verify_order_fill(
        self,
        symbol: str,
        expected_change: float,
        position_before: float,
        max_wait_seconds: float = 3.0
    ) -> Dict:
        """
        Verify that an order actually filled by checking position change

        Args:
            symbol: Market symbol
            expected_change: Expected position change (positive for buy, negative for sell)
            position_before: Position size before order was placed
            max_wait_seconds: Maximum time to wait for fill confirmation

        Returns:
            Dict with 'filled' boolean and 'position_after' or 'error' message
        """
        import asyncio

        # Wait briefly for order to process
        await asyncio.sleep(0.5)

        # Check position multiple times
        checks = int(max_wait_seconds / 0.5)
        for i in range(checks):
            position_after = await self.get_position_size(symbol)
            actual_change = position_after - position_before

            # Check if position changed in expected direction
            # Allow for some tolerance due to partial fills or rounding
            if expected_change > 0:  # Buy order
                if actual_change > expected_change * 0.8:  # 80% of expected is good enough
                    logger.info(f"✅ Order VERIFIED: position changed {position_before:.6f} → {position_after:.6f}")
                    return {'filled': True, 'position_after': position_after, 'actual_change': actual_change}
            else:  # Sell order
                if actual_change < expected_change * 0.8:
                    logger.info(f"✅ Order VERIFIED: position changed {position_before:.6f} → {position_after:.6f}")
                    return {'filled': True, 'position_after': position_after, 'actual_change': actual_change}

            if i < checks - 1:
                await asyncio.sleep(0.5)

        # Order did not fill
        logger.error(f"❌ Order NOT FILLED: position unchanged at {position_before:.6f} (expected change: {expected_change:.6f})")
        return {
            'filled': False,
            'error': 'Order accepted but not filled - likely rejected due to insufficient margin',
            'position_before': position_before,
            'position_after': position_after
        }

    async def get_orderbook(self, symbol: str) -> Optional[Dict]:
        """
        Get orderbook for a specific market

        Args:
            symbol: Market symbol (e.g., "BTC/USDT-P")

        Returns:
            Orderbook with bids and asks
        """
        try:
            response = await self._request("GET", "/market/data/orderbook", params={"symbol": symbol}, use_data_api=True)
            if "error" in response:
                logger.error(f"Failed to get orderbook: {response['error']}")
                return None
            return response
        except Exception as e:
            logger.error(f"Error getting orderbook: {e}")
            return None

    async def get_price(self, symbol: str) -> Optional[float]:
        """
        Get current price for a specific market

        Args:
            symbol: Market symbol (e.g., "BTC/USDT-P")

        Returns:
            Current price or None
        """
        try:
            response = await self._request("GET", "/market/data/prices", params={"symbol": symbol}, use_data_api=True)
            if "error" in response:
                logger.error(f"Failed to get price: {response['error']}")
                return None

            # Parse price from response (exact field TBD)
            price = response.get("price") or response.get("lastPrice") or response.get("markPrice")
            if price is not None:
                return float(price)

            logger.warning(f"Price not found in response: {response}")
            return None

        except Exception as e:
            logger.error(f"Error getting price: {e}")
            return None

    async def get_market_info(self, symbol: str) -> Optional[Dict]:
        """
        Get market info for a specific symbol

        Args:
            symbol: Market symbol (e.g., "SOL/USDT-P")

        Returns:
            Market info dict with id, decimals, etc.
        """
        markets = await self.get_markets()
        for market in markets:
            if market.get("symbol") == symbol:
                return market
        logger.error(f"Market not found: {symbol}")
        return None

    def _pack_order_buffer(
        self,
        nonce: int,
        contract_id: int,
        quantity: int,
        side: int,
        price: Optional[int],
        max_fees: int
    ) -> bytes:
        """
        Pack order fields into binary buffer for HMAC signature

        Args:
            nonce: Millisecond timestamp (8 bytes)
            contract_id: Market contract ID (4 bytes)
            quantity: Order quantity with decimals (8 bytes)
            side: 0=ASK (sell), 1=BID (buy) (4 bytes)
            price: Price with decimals (8 bytes, None for market orders)
            max_fees: Max fees in basis points (8 bytes)

        Returns:
            Binary buffer ready for HMAC signing
        """
        # Pack all fields as big-endian unsigned integers
        # Q = unsigned long long (8 bytes)
        # I = unsigned int (4 bytes)
        buffer = struct.pack('>Q', nonce)           # 8 bytes
        buffer += struct.pack('>I', contract_id)    # 4 bytes
        buffer += struct.pack('>Q', quantity)       # 8 bytes
        buffer += struct.pack('>I', side)           # 4 bytes
        # Price is optional - omit for market orders
        if price is not None:
            buffer += struct.pack('>Q', price)      # 8 bytes
        buffer += struct.pack('>Q', max_fees)       # 8 bytes
        return buffer

    def _sign_order_buffer(self, buffer: bytes) -> str:
        """
        Sign binary buffer with HMAC-SHA256

        Args:
            buffer: Binary buffer to sign

        Returns:
            Hex signature string
        """
        signature = hmac.new(
            self.api_secret_bytes,
            buffer,
            hashlib.sha256
        ).hexdigest()
        return signature

    def _sign_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Dict[str, str]:
        """
        Generate HMAC signature for WRITE operations

        Args:
            method: HTTP method (POST, DELETE)
            endpoint: API endpoint path
            data: Request body data

        Returns:
            Dictionary of headers with signature
        """
        timestamp = str(int(time.time() * 1000))

        # Build message to sign: timestamp + method + endpoint + body
        message = timestamp + method + endpoint
        if data:
            message += json.dumps(data, separators=(',', ':'))

        # Generate HMAC-SHA256 signature
        signature = hmac.new(
            self.api_secret_bytes,
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        return {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
            "Timestamp": timestamp,
            "Signature": signature
        }

    async def create_market_order(
        self,
        symbol: str,
        is_buy: bool,
        amount: float
    ) -> Optional[Dict]:
        """
        Create a market order using binary buffer signature

        Args:
            symbol: Market symbol (e.g., "BTC/USDT-P")
            is_buy: True for buy, False for sell
            amount: Order size in base currency

        Returns:
            Order response or None
        """
        try:
            # Get account ID
            account_id = self.get_account_id()
            if not account_id:
                logger.error("Cannot create order: account ID not set. Use set_account_id() or pass to constructor.")
                return None

            # Get market info to find contract ID and decimals
            market_info = await self.get_market_info(symbol)
            if not market_info:
                logger.error(f"Cannot create order: market info not found for {symbol}")
                return None

            contract_id = market_info.get("id")
            underlying_decimals = market_info.get("underlyingDecimals", 8)  # Default to 8 for SOL

            if contract_id is None:
                logger.error(f"Cannot create order: contract ID not found for {symbol}")
                return None

            # Generate nonce
            nonce = int(time.time() * 1000)

            # Convert quantity to integer with proper decimals
            # Quantity = amount × 10^decimals
            quantity_int = int(amount * (10 ** underlying_decimals))

            # Side: 0=ASK (sell), 1=BID (buy)
            side_int = 1 if is_buy else 0

            # Price: None for market orders (omit from buffer)
            price_int = None

            # Max fees: Per official SDK - rate × 10^8
            # 0.5% = 0.005 → 0.005 × 10^8 = 500000
            max_fees_int = int(0.005 * (10 ** 8))

            # Pack binary buffer for signature
            buffer = self._pack_order_buffer(
                nonce=nonce,
                contract_id=contract_id,
                quantity=quantity_int,
                side=side_int,
                price=price_int,  # None for market orders
                max_fees=max_fees_int
            )

            # Sign the buffer
            signature = self._sign_order_buffer(buffer)

            # Build order request
            # CRITICAL: quantity_str MUST match quantity_int exactly!
            # The server recalculates quantity_int from quantity_str for signature verification.
            # Compute quantity_str from quantity_int to ensure exact match.
            quantity_str = f"{quantity_int / (10 ** underlying_decimals):.{underlying_decimals}f}".rstrip('0').rstrip('.')

            order_data = {
                "accountId": int(account_id),
                "symbol": symbol,
                "side": "BID" if is_buy else "ASK",
                "orderType": "MARKET",
                "quantity": quantity_str,
                "nonce": nonce,
                "maxFeesPercent": "0.00500000",
                "signature": signature  # Include binary buffer signature
            }

            # Simple Authorization header for POST (not timestamp-based)
            headers = self._get_headers()
            endpoint = "/trade/order"
            url = f"{self.base_url}{endpoint}"

            logger.info(f"Creating order: {symbol} {'BUY' if is_buy else 'SELL'} {amount}")
            logger.debug(f"Order data: {order_data}")

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=order_data) as resp:
                    if resp.status == 200:
                        response = await resp.json()
                        logger.info(f"✅ Order created: {response}")
                        return response
                    else:
                        error_text = await resp.text()
                        logger.error(f"Failed to create order - API Error {resp.status}: {error_text}")
                        # Return error dict so executor can detect and retry
                        return {'error': error_text, 'status': resp.status}

        except Exception as e:
            logger.error(f"Error creating order: {e}")
            return {'error': str(e)}

    async def create_limit_order(
        self,
        symbol: str,
        is_buy: bool,
        amount: float,
        price: float
    ) -> Optional[Dict]:
        """
        Create a limit order using binary buffer signature

        Note: POST_ONLY (ALO) is documented in Hibachi conceptual docs but not exposed
        in the API. Use wide spreads (15-20 bps) to ensure orders rest on book.

        Args:
            symbol: Market symbol (e.g., "BTC/USDT-P")
            is_buy: True for buy, False for sell
            amount: Order size in base currency
            price: Limit price

        Returns:
            Order response or None
        """
        try:
            # Get account ID
            account_id = self.get_account_id()
            if not account_id:
                logger.error("Cannot create order: account ID not set.")
                return None

            # Get market info
            market_info = await self.get_market_info(symbol)
            if not market_info:
                logger.error(f"Cannot create order: market info not found for {symbol}")
                return None

            contract_id = market_info.get("id")
            underlying_decimals = market_info.get("underlyingDecimals", 8)
            settlement_decimals = market_info.get("settlementDecimals", 6)

            if contract_id is None:
                logger.error(f"Cannot create order: contract ID not found for {symbol}")
                return None

            # Generate nonce
            nonce = int(time.time() * 1000)

            # Convert quantity to integer with underlying decimals
            quantity_int = int(amount * (10 ** underlying_decimals))

            # Price formula from Hibachi docs: price * 2^32 * 10^(settlementDecimals - underlyingDecimals)
            # This uses fixed-point representation with 32 bits after decimal point
            price_exponent = settlement_decimals - underlying_decimals
            price_int = int(price * (2**32) * (10 ** price_exponent))

            # Side: 0=ASK (sell), 1=BID (buy)
            side_int = 1 if is_buy else 0

            # Max fees
            max_fees_int = int(0.005 * (10 ** 8))

            # Pack binary buffer with price for limit order
            buffer = self._pack_order_buffer(
                nonce=nonce,
                contract_id=contract_id,
                quantity=quantity_int,
                side=side_int,
                price=price_int,
                max_fees=max_fees_int
            )

            # Sign the buffer
            signature = self._sign_order_buffer(buffer)

            # Build order request
            quantity_str = f"{quantity_int / (10 ** underlying_decimals):.{underlying_decimals}f}".rstrip('0').rstrip('.')
            # Price string should match the actual price (human readable)
            price_str = f"{price:.6f}".rstrip('0').rstrip('.')

            order_data = {
                "accountId": int(account_id),
                "symbol": symbol,
                "side": "BID" if is_buy else "ASK",
                "orderType": "LIMIT",
                "quantity": quantity_str,
                "price": price_str,
                "nonce": nonce,
                "maxFeesPercent": "0.00500000",
                "signature": signature
            }

            headers = self._get_headers()
            endpoint = "/trade/order"
            url = f"{self.base_url}{endpoint}"

            logger.info(f"Creating LIMIT order: {symbol} {'BUY' if is_buy else 'SELL'} {amount} @ ${price}")

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=order_data) as resp:
                    if resp.status == 200:
                        response = await resp.json()
                        logger.info(f"✅ Limit order created: {response}")
                        return response
                    else:
                        error_text = await resp.text()
                        logger.error(f"Failed to create limit order - API Error {resp.status}: {error_text}")
                        return {'error': error_text, 'status': resp.status}

        except Exception as e:
            logger.error(f"Error creating limit order: {e}")
            return {'error': str(e)}

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order

        Args:
            order_id: Order ID to cancel

        Returns:
            True if successful
        """
        try:
            # Get account ID
            account_id = self.get_account_id()
            if not account_id:
                logger.error("Cannot cancel order: account ID not set. Use set_account_id() or pass to constructor.")
                return False

            # Per Hibachi docs: Cancel signature is orderId as 64-bit big-endian integer
            # "If you are using orderId, the payload will be the orderId as a 64-bit integer"
            order_id_int = int(order_id)
            cancel_buffer = struct.pack('>Q', order_id_int)  # 8 bytes big-endian
            signature = self._sign_order_buffer(cancel_buffer)

            cancel_data = {
                "accountId": int(account_id),
                "orderId": order_id,
                "signature": signature
            }

            headers = self._get_headers()
            endpoint = "/trade/order"
            url = f"{self.base_url}{endpoint}"

            async with aiohttp.ClientSession() as session:
                async with session.delete(url, headers=headers, json=cancel_data) as resp:
                    if resp.status == 200:
                        logger.info(f"✅ Order {order_id} cancelled")
                        return True
                    else:
                        error_text = await resp.text()
                        logger.error(f"Failed to cancel order - API Error {resp.status}: {error_text}")
                        return False

        except Exception as e:
            logger.error(f"Error canceling order: {e}")
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """
        Cancel all open orders, optionally for a specific symbol

        Args:
            symbol: Optional symbol filter (e.g., "BTC/USDT-P")

        Returns:
            Number of orders cancelled
        """
        try:
            orders = await self.get_orders(symbol)
            # Hibachi uses 'PLACED' status for open orders, not 'OPEN'
            open_orders = [o for o in orders if o.get('status') in ['OPEN', 'PLACED']]

            if not open_orders:
                return 0

            cancelled = 0
            for order in open_orders:
                order_id = order.get('orderId')
                if order_id:
                    if await self.cancel_order(str(order_id)):
                        cancelled += 1

            logger.info(f"Cancelled {cancelled}/{len(open_orders)} orders")
            return cancelled

        except Exception as e:
            logger.error(f"Error cancelling all orders: {e}")
            return 0

    async def get_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """
        Get all orders (open and recent)

        Args:
            symbol: Optional symbol filter (e.g., "BTC/USDT-P")

        Returns:
            List of order dictionaries
        """
        try:
            # Get account ID
            account_id = self.get_account_id()
            if not account_id:
                logger.error("Cannot get orders: account ID not set. Use set_account_id() or pass to constructor.")
                return []

            params = {"accountId": account_id}
            if symbol:
                params["symbol"] = symbol

            response = await self._request("GET", "/trade/orders", params=params)
            if "error" in response:
                logger.error(f"Failed to get orders: {response['error']}")
                return []

            # Response may be direct list or dict with "orders" key
            if isinstance(response, list):
                return response
            return response.get("orders", [])

        except Exception as e:
            logger.error(f"Error getting orders: {e}")
            return []


async def test_connection():
    """Test Hibachi SDK connection"""
    api_key = os.getenv("HIBACHI_PUBLIC_KEY")
    api_secret = os.getenv("HIBACHI_PRIVATE_KEY")
    account_id = os.getenv("HIBACHI_ACCOUNT_ID")

    if not api_key or not api_secret:
        print("❌ Missing API keys in .env")
        return

    print(f"Testing Hibachi connection...")
    print(f"API Key: {api_key[:20]}...")

    sdk = HibachiSDK(api_key, api_secret, account_id)

    # Test 1: Check account ID
    print("\n1️⃣ Checking account ID...")
    if sdk.get_account_id():
        print(f"✅ Account ID: {sdk.get_account_id()}")
    else:
        print("⚠️  Account ID not set. Add HIBACHI_ACCOUNT_ID to .env")
        print("   Get it from Hibachi UI: Settings → API Keys")
        print("   Continuing with tests that don't require account ID...")

    # Test 2: Get markets
    print("\n2️⃣ Testing get_markets()...")
    markets = await sdk.get_markets()
    if markets:
        print(f"✅ Found {len(markets)} markets")
        for market in markets[:5]:
            print(f"   - {market}")
    else:
        print("❌ Failed to get markets")

    # Test 3: Get balance
    print("\n3️⃣ Testing get_balance()...")
    balance = await sdk.get_balance()
    if balance is not None:
        print(f"✅ Balance: ${balance:.2f}")
    else:
        print("❌ Failed to get balance")

    # Test 4: Get positions
    print("\n4️⃣ Testing get_positions()...")
    positions = await sdk.get_positions()
    print(f"✅ Open positions: {len(positions)}")
    for pos in positions:
        print(f"   - {pos}")

    # Test 5: Get orders
    print("\n5️⃣ Testing get_orders()...")
    orders = await sdk.get_orders()
    print(f"✅ Orders: {len(orders)}")
    for order in orders[:3]:
        print(f"   - {order}")

    # Test 6: Get SOL price
    print("\n6️⃣ Testing get_price('SOL/USDT-P')...")
    sol_price = await sdk.get_price("SOL/USDT-P")
    if sol_price:
        print(f"✅ SOL Price: ${sol_price:.2f}")
    else:
        print("❌ Failed to get SOL price")

    # Test 7: Get SOL orderbook
    print("\n7️⃣ Testing get_orderbook('SOL/USDT-P')...")
    orderbook = await sdk.get_orderbook("SOL/USDT-P")
    if orderbook:
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        print(f"✅ Orderbook - Bids: {len(bids)}, Asks: {len(asks)}")
        if bids:
            print(f"   Best bid: {bids[0]}")
        if asks:
            print(f"   Best ask: {asks[0]}")
    else:
        print("❌ Failed to get orderbook")


if __name__ == "__main__":
    asyncio.run(test_connection())
