"""
Nado DEX SDK Wrapper
EIP-712 signing with Linked Signer authentication

Nado uses a "Linked Signer" pattern for authentication:
- Main wallet address identifies the subaccount
- Linked signer private key signs all trading operations
- This keeps the main wallet secure while allowing bot trading

Setup:
1. Generate a new random private key (the "linked signer")
2. Enable "1-Click Trading" in Nado UI with your linked signer address
3. Store the linked signer private key in .env as NADO_LINKED_SIGNER_PRIVATE_KEY
4. Store your main wallet address as NADO_WALLET_ADDRESS
"""

import asyncio
import aiohttp
import json
import os
import logging
import time
from typing import Dict, Optional, List, Union
from decimal import Decimal
from dotenv import load_dotenv

# Web3/eth dependencies for EIP-712 signing
from eth_account import Account
from eth_account.messages import encode_typed_data

load_dotenv()

logger = logging.getLogger(__name__)


class NadoSDK:
    """
    REST API wrapper for Nado trading

    Uses Linked Signer authentication pattern (like API keys in other exchanges):
    - wallet_address: Identifies your subaccount (like API key)
    - linked_signer_private_key: Signs trades (like API secret)
    """

    # EIP-712 Domain for Nado
    DOMAIN_NAME = "Nado"
    DOMAIN_VERSION = "0.0.1"

    # Chain IDs
    MAINNET_CHAIN_ID = 57073  # Ink mainnet
    TESTNET_CHAIN_ID = 763373  # Ink testnet

    # Contract addresses (verifying contract for EIP-712)
    MAINNET_VERIFYING_CONTRACT = "0x05ec92d78ed421f3d3ada77ffde167106565974e"
    TESTNET_VERIFYING_CONTRACT = "0x0000000000000000000000000000000000000001"  # Placeholder

    # Product ID to symbol mapping (from Nado symbols endpoint)
    PRODUCT_SYMBOLS = {
        2: "BTC-PERP",
        4: "ETH-PERP",
        8: "SOL-PERP",
        10: "XRP-PERP",
        14: "BNB-PERP",
        16: "HYPE-PERP",
        18: "ZEC-PERP",
        20: "MON-PERP",
        22: "FARTCOIN-PERP",
        24: "SUI-PERP",
        26: "AAVE-PERP",
        28: "XAUT-PERP",  # Gold
        30: "PUMP-PERP",
        32: "TAO-PERP",
        34: "XMR-PERP",
        36: "LIT-PERP",
        38: "kPEPE-PERP",
        40: "PENGU-PERP",
        42: "USELESS-PERP",
        44: "SKR-PERP",
        46: "UNI-PERP",
        48: "ASTER-PERP",
        50: "XPL-PERP",
        52: "DOGE-PERP",
        54: "WLFI-PERP",
        56: "kBONK-PERP",
    }

    def __init__(
        self,
        wallet_address: str,
        linked_signer_private_key: str,
        subaccount_name: str = "default",
        testnet: bool = False
    ):
        """
        Initialize Nado SDK

        Args:
            wallet_address: Main wallet address (identifies the subaccount)
            linked_signer_private_key: Private key of the linked signer (for signing)
            subaccount_name: Subaccount name (default: "default", max 12 chars)
            testnet: Use testnet endpoints if True
        """
        self.wallet_address = wallet_address
        self.subaccount_name = subaccount_name[:12]  # Max 12 chars
        self.testnet = testnet

        # Initialize signer from linked signer private key
        self.signer = Account.from_key(linked_signer_private_key)
        logger.info(f"Nado SDK initialized - Wallet: {wallet_address[:10]}..., Signer: {self.signer.address[:10]}...")

        # Set endpoints based on network
        if testnet:
            self.rest_url = "https://gateway.test.nado.xyz/v1"
            self.ws_url = "wss://gateway.test.nado.xyz/v1/ws"
            self.archive_url = "https://archive.test.nado.xyz/v1"
            self.chain_id = self.TESTNET_CHAIN_ID
            self.verifying_contract = self.TESTNET_VERIFYING_CONTRACT
        else:
            self.rest_url = "https://gateway.prod.nado.xyz/v1"
            self.ws_url = "wss://gateway.prod.nado.xyz/v1/ws"
            self.archive_url = "https://archive.prod.nado.xyz/v1"
            self.chain_id = self.MAINNET_CHAIN_ID
            self.verifying_contract = self.MAINNET_VERIFYING_CONTRACT

        # Cache for products
        self._products_cache: Optional[List[Dict]] = None
        self._products_cache_time: float = 0

    def _get_subaccount_bytes32(self) -> str:
        """
        Convert wallet address + subaccount name to bytes32 hex string

        Format: address (20 bytes) + name (12 bytes, right-padded with zeros)
        """
        # Remove 0x prefix and ensure lowercase
        address_hex = self.wallet_address.lower().replace('0x', '')

        # Pad address to 20 bytes (40 hex chars)
        address_hex = address_hex.zfill(40)

        # Encode subaccount name as bytes (max 12 chars) and pad
        name_bytes = self.subaccount_name.encode('utf-8')[:12]
        name_hex = name_bytes.hex().ljust(24, '0')  # 12 bytes = 24 hex chars

        return f"0x{address_hex}{name_hex}"

    def _get_signer_bytes32(self) -> str:
        """
        Convert signer address to bytes32 (left-padded with zeros)
        """
        address_hex = self.signer.address.lower().replace('0x', '')
        return f"0x{address_hex.zfill(64)}"

    def _to_x18(self, value: float) -> int:
        """Convert float to x18 decimal integer"""
        return int(Decimal(str(value)) * Decimal(10**18))

    def _from_x18(self, value: int) -> float:
        """Convert x18 decimal integer to float"""
        return float(Decimal(value) / Decimal(10**18))

    def _generate_nonce(self, recv_time_delta_ms: int = 30000) -> int:
        """
        Generate nonce for Nado order

        The nonce encodes two pieces of information:
        - Most significant 44 bits: recv_time (ms) - time after which order is discarded
        - Least significant 20 bits: random integer to avoid hash collisions

        Args:
            recv_time_delta_ms: How long the order should be valid (default 30 seconds)

        Returns:
            Nonce as integer
        """
        import random
        # recv_time is a FUTURE timestamp - when the order should be discarded
        recv_time_ms = int(time.time() * 1000) + recv_time_delta_ms
        random_bits = random.getrandbits(20)
        return (recv_time_ms << 20) + random_bits

    def _build_order_appendix(
        self,
        order_type: str = "DEFAULT",
        isolated: bool = False,
        reduce_only: bool = False
    ) -> int:
        """
        Build order appendix (128-bit value encoding order options)

        Bit layout:
        | value (64) | reserved (50) | trigger (2) | reduce_only (1) | order_type (2) | isolated (1) | version (8) |
        """
        version = 1
        isolated_bit = 1 if isolated else 0

        # Order type: DEFAULT=0, IOC=1, FOK=2, POST_ONLY=3
        order_type_map = {"DEFAULT": 0, "IOC": 1, "FOK": 2, "POST_ONLY": 3}
        order_type_bits = order_type_map.get(order_type.upper(), 0)

        reduce_only_bit = 1 if reduce_only else 0
        trigger_bits = 0  # Not using trigger orders for now

        # Pack the bits
        appendix = version
        appendix |= (isolated_bit << 8)
        appendix |= (order_type_bits << 9)
        appendix |= (reduce_only_bit << 11)
        appendix |= (trigger_bits << 12)

        return appendix

    def _get_eip712_domain(self, product_id: Optional[int] = None) -> Dict:
        """
        Get EIP-712 domain for signing

        Args:
            product_id: For order signing, use address(productId) as verifyingContract.
                       For other operations, use the endpoint address.
        """
        if product_id is not None:
            # For orders: verifyingContract = address(productId)
            verifying_contract = self._product_id_to_address(product_id)
        else:
            # For other executes: use endpoint address
            verifying_contract = self.verifying_contract

        return {
            "name": self.DOMAIN_NAME,
            "version": self.DOMAIN_VERSION,
            "chainId": self.chain_id,
            "verifyingContract": verifying_contract
        }

    def _product_id_to_address(self, product_id: int) -> str:
        """
        Convert product ID to 20-byte address format for order signing.
        Per Nado docs: verifyingContract for orders = address(productId)
        """
        be_bytes = product_id.to_bytes(20, byteorder="big", signed=False)
        return "0x" + be_bytes.hex()

    def _sign_order(
        self,
        product_id: int,
        price_x18: int,
        amount_x18: int,
        expiration: int,
        nonce: int,
        appendix: int
    ) -> str:
        """
        Sign an order using EIP-712 typed data

        Args:
            product_id: Product ID from Nado
            price_x18: Price in x18 format
            amount_x18: Amount in x18 format (positive=buy, negative=sell)
            expiration: Unix timestamp for order expiration
            nonce: Order nonce
            appendix: Order options appendix

        Returns:
            Hex signature string
        """
        # Build EIP-712 typed data
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"}
                ],
                "Order": [
                    {"name": "sender", "type": "bytes32"},
                    {"name": "priceX18", "type": "int128"},
                    {"name": "amount", "type": "int128"},
                    {"name": "expiration", "type": "uint64"},
                    {"name": "nonce", "type": "uint64"},
                    {"name": "appendix", "type": "uint128"}
                ]
            },
            "primaryType": "Order",
            "domain": self._get_eip712_domain(product_id=product_id),  # Use address(productId)
            "message": {
                "sender": self._get_subaccount_bytes32(),
                "priceX18": price_x18,
                "amount": amount_x18,
                "expiration": expiration,
                "nonce": nonce,
                "appendix": appendix
            }
        }

        # Sign with linked signer
        signable_message = encode_typed_data(full_message=typed_data)
        signed = self.signer.sign_message(signable_message)

        return signed.signature.hex()

    def _sign_cancel(self, product_ids: List[int], digests: List[str], nonce: int) -> str:
        """
        Sign a cancel order request using EIP-712

        NOTE: Uses same string format as working order signing
        """
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"}
                ],
                "Cancellation": [
                    {"name": "sender", "type": "bytes32"},
                    {"name": "productIds", "type": "uint32[]"},
                    {"name": "digests", "type": "bytes32[]"},
                    {"name": "nonce", "type": "uint64"}
                ]
            },
            "primaryType": "Cancellation",
            "domain": self._get_eip712_domain(),
            "message": {
                "sender": self._get_subaccount_bytes32(),
                "productIds": product_ids,
                "digests": digests,
                "nonce": nonce
            }
        }

        signable_message = encode_typed_data(full_message=typed_data)
        signed = self.signer.sign_message(signable_message)

        return signed.signature.hex()

    def _sign_link_signer(self, signer_address: str, nonce: int) -> str:
        """
        Sign a link signer request (for initial setup)
        NOTE: This must be signed by the MAIN WALLET, not the linked signer
        """
        # Convert signer address to bytes32 format
        signer_bytes32 = f"0x{signer_address.lower().replace('0x', '').zfill(64)}"

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"}
                ],
                "LinkSigner": [
                    {"name": "sender", "type": "bytes32"},
                    {"name": "signer", "type": "bytes32"},
                    {"name": "nonce", "type": "uint64"}
                ]
            },
            "primaryType": "LinkSigner",
            "domain": self._get_eip712_domain(),
            "message": {
                "sender": self._get_subaccount_bytes32(),
                "signer": signer_bytes32,
                "nonce": nonce
            }
        }

        signable_message = encode_typed_data(full_message=typed_data)
        signed = self.signer.sign_message(signable_message)

        return signed.signature.hex()

    async def _query(self, query_type: str, params: Optional[Dict] = None) -> Dict:
        """
        Make a query request to Nado API

        Args:
            query_type: Query type (e.g., "all_products", "subaccount_info")
            params: Additional query parameters

        Returns:
            JSON response
        """
        url = f"{self.rest_url}/query"

        # Build query params
        query_params = {"type": query_type}
        if params:
            query_params.update(params)

        headers = {"Accept-Encoding": "gzip, deflate"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=query_params, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        error_text = await resp.text()
                        logger.error(f"Query Error {resp.status}: {error_text}")
                        return {"status": "failure", "error": error_text}

        except Exception as e:
            logger.error(f"Query failed: {e}")
            return {"status": "failure", "error": str(e)}

    async def _execute(self, execute_type: str, payload: Dict) -> Dict:
        """
        Make an execute request to Nado API

        Args:
            execute_type: Execute type (e.g., "place_order", "cancel_orders")
            payload: Request payload including signature

        Returns:
            JSON response
        """
        url = f"{self.rest_url}/execute"

        # Wrap payload with execute type
        request_body = {execute_type: payload}

        headers = {"Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=request_body) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        error_text = await resp.text()
                        logger.error(f"Execute Error {resp.status}: {error_text}")
                        return {"status": "failure", "error": error_text}

        except Exception as e:
            logger.error(f"Execute failed: {e}")
            return {"status": "failure", "error": str(e)}

    # ===== Public Query Methods =====

    async def get_products(self, force_refresh: bool = False) -> List[Dict]:
        """
        Get all available products (markets)

        Returns:
            List of product info dicts with id, symbol, etc.
        """
        # Check cache (5 minute TTL)
        if not force_refresh and self._products_cache and (time.time() - self._products_cache_time < 300):
            return self._products_cache

        response = await self._query("all_products")

        if response.get("status") == "success":
            data = response.get("data", {})
            # Nado returns perp_products and spot_products arrays
            perp_products = data.get("perp_products", [])

            # Add symbols from our mapping
            for p in perp_products:
                product_id = p.get("product_id")
                p["symbol"] = self.PRODUCT_SYMBOLS.get(product_id, f"UNKNOWN-{product_id}")
                # Parse oracle price for convenience
                oracle_price_x18 = p.get("oracle_price_x18", "0")
                p["oracle_price"] = self._from_x18(int(oracle_price_x18))
                # Parse book_info increments
                bi = p.get("book_info", {})
                p["price_increment"] = self._from_x18(int(bi.get("price_increment_x18", "0")))
                p["size_increment"] = self._from_x18(int(bi.get("size_increment", "0")))
                p["min_size"] = self._from_x18(int(bi.get("min_size", "0")))

            self._products_cache = perp_products
            self._products_cache_time = time.time()
            return perp_products
        else:
            logger.error(f"Failed to get products: {response}")
            return []

    async def get_product_by_symbol(self, symbol: str) -> Optional[Dict]:
        """
        Get product info by symbol (e.g., "BTC-PERP", "ETH-PERP")
        """
        products = await self.get_products()
        for p in products:
            if p.get("symbol") == symbol:
                return p
        return None

    async def get_subaccount_info(self) -> Optional[Dict]:
        """
        Get subaccount information including balances and positions
        """
        response = await self._query("subaccount_info", {
            "subaccount": self._get_subaccount_bytes32()
        })

        if response.get("status") == "success":
            return response.get("data")
        else:
            logger.error(f"Failed to get subaccount info: {response}")
            return None

    async def get_balance(self) -> Optional[float]:
        """
        Get available USDT0 balance
        """
        info = await self.get_subaccount_info()
        if info:
            # Balance is in spot_balances array - product_id 0 is USDT0
            spot_balances = info.get("spot_balances", [])
            for bal in spot_balances:
                if bal.get("product_id") == 0:
                    amount = bal.get("balance", {}).get("amount", "0")
                    return self._from_x18(int(amount))
            return 0.0
        return None

    async def get_positions(self) -> List[Dict]:
        """
        Get all open positions (non-zero perp balances)
        """
        info = await self.get_subaccount_info()
        if info:
            positions = []
            perp_balances = info.get("perp_balances", [])
            for bal in perp_balances:
                amount = int(bal.get("balance", {}).get("amount", "0"))
                if amount != 0:
                    product_id = bal.get("product_id")
                    positions.append({
                        "product_id": product_id,
                        "symbol": self.PRODUCT_SYMBOLS.get(product_id, f"UNKNOWN-{product_id}"),
                        "amount": amount,
                        "amount_float": self._from_x18(amount),
                        "v_quote_balance": bal.get("balance", {}).get("v_quote_balance", "0")
                    })
            return positions
        return []

    async def get_position_size(self, symbol: str) -> float:
        """
        Get position size for a specific symbol

        Returns:
            Position size (positive for long, negative for short, 0 if no position)
        """
        positions = await self.get_positions()
        for p in positions:
            if p.get("symbol") == symbol:
                return p.get("amount_float", 0.0)
        return 0.0

    async def get_orders(self, product_id: Optional[int] = None) -> List[Dict]:
        """
        Get open orders
        """
        params = {"sender": self._get_subaccount_bytes32()}
        if product_id is not None:
            params["product_id"] = str(product_id)

        response = await self._query("subaccount_orders", params)

        if response.get("status") == "success":
            return response.get("data", {}).get("orders", [])
        return []

    async def get_linked_signer(self) -> Optional[str]:
        """
        Get current linked signer address for this subaccount
        """
        response = await self._query("linked_signer", {
            "subaccount": self._get_subaccount_bytes32()
        })

        if response.get("status") == "success":
            signer = response.get("data", {}).get("linked_signer", "")
            if signer and signer != "0x0000000000000000000000000000000000000000":
                return signer
        return None

    async def get_nonce(self) -> int:
        """
        Get current nonce for this subaccount (for link_signer operations)
        """
        response = await self._query("nonces", {
            "address": self._get_subaccount_bytes32()
        })

        if response.get("status") == "success":
            return int(response.get("data", {}).get("tx_nonce", 0))
        return 0

    # ===== Execute Methods =====

    async def create_market_order(
        self,
        symbol: str,
        is_buy: bool,
        amount: float,
        reduce_only: bool = False
    ) -> Optional[Dict]:
        """
        Create a market order

        Args:
            symbol: Product symbol (e.g., "BTC-PERP")
            is_buy: True for buy/long, False for sell/short
            amount: Order size in base currency
            reduce_only: If True, only reduce existing position

        Returns:
            Order response or None
        """
        try:
            # Get product info
            product = await self.get_product_by_symbol(symbol)
            if not product:
                logger.error(f"Product not found: {symbol}")
                return None

            product_id = product.get("product_id", product.get("id"))

            # Convert amount to x18 (positive for buy, negative for sell)
            amount_x18 = self._to_x18(amount)
            if not is_buy:
                amount_x18 = -amount_x18

            # Market orders use aggressive price (IOC will fill at best available)
            # Buy: use high price (200% of oracle to ensure fill)
            # Sell: use low price (50% of oracle to ensure fill)
            oracle_price = product.get("oracle_price", 0)
            if is_buy:
                price = oracle_price * 2.0  # 200% of oracle
            else:
                price = oracle_price * 0.5  # 50% of oracle

            # Round price to price_increment
            price_increment_x18 = int(product.get("book_info", {}).get("price_increment_x18", "100000000000000000"))
            price_x18 = self._to_x18(price)
            price_x18 = (price_x18 // price_increment_x18) * price_increment_x18

            # Generate order params
            nonce = self._generate_nonce()
            expiration = int(time.time()) + 300  # 5 minutes - rely on cancel to manage orders
            appendix = self._build_order_appendix(
                order_type="IOC",  # Immediate or Cancel for market orders
                reduce_only=reduce_only
            )

            # Sign the order
            signature = self._sign_order(
                product_id=product_id,
                price_x18=price_x18,
                amount_x18=amount_x18,
                expiration=expiration,
                nonce=nonce,
                appendix=appendix
            )

            # Build execute payload
            payload = {
                "order": {
                    "sender": self._get_subaccount_bytes32(),
                    "priceX18": str(price_x18),
                    "amount": str(amount_x18),
                    "expiration": str(expiration),
                    "nonce": str(nonce),
                    "appendix": str(appendix)
                },
                "product_id": product_id,
                "signature": signature  # Already has 0x prefix from HexBytes.hex()
            }

            logger.info(f"Creating market order: {symbol} {'BUY' if is_buy else 'SELL'} {amount}")

            response = await self._execute("place_order", payload)

            if response.get("status") == "success":
                logger.info(f"✅ Order created: {response}")
                return response
            else:
                logger.error(f"❌ Order failed: {response}")
                return response

        except Exception as e:
            logger.error(f"Error creating market order: {e}")
            return {"status": "failure", "error": str(e)}

    async def create_limit_order(
        self,
        symbol: str,
        is_buy: bool,
        amount: float,
        price: float,
        order_type: str = "DEFAULT",
        reduce_only: bool = False
    ) -> Optional[Dict]:
        """
        Create a limit order

        Args:
            symbol: Product symbol (e.g., "BTC-PERP")
            is_buy: True for buy/long, False for sell/short
            amount: Order size in base currency
            price: Limit price
            order_type: DEFAULT, IOC, FOK, or POST_ONLY
            reduce_only: If True, only reduce existing position

        Returns:
            Order response or None
        """
        try:
            # Get product info
            product = await self.get_product_by_symbol(symbol)
            if not product:
                logger.error(f"Product not found: {symbol}")
                return None

            product_id = product.get("product_id", product.get("id"))

            # Convert to x18
            amount_x18 = self._to_x18(amount)
            if not is_buy:
                amount_x18 = -amount_x18
            price_x18 = self._to_x18(price)

            # Snap price to price_increment to avoid float→x18 rounding errors
            price_increment_x18 = int(product.get("book_info", {}).get("price_increment_x18", "100000000000000000"))
            price_x18 = (price_x18 // price_increment_x18) * price_increment_x18

            # Generate order params
            nonce = self._generate_nonce()
            expiration = int(time.time()) + 300  # 5 minutes - rely on cancel to manage orders
            appendix = self._build_order_appendix(
                order_type=order_type,
                reduce_only=reduce_only
            )

            # Sign the order
            signature = self._sign_order(
                product_id=product_id,
                price_x18=price_x18,
                amount_x18=amount_x18,
                expiration=expiration,
                nonce=nonce,
                appendix=appendix
            )

            # Build execute payload
            payload = {
                "order": {
                    "sender": self._get_subaccount_bytes32(),
                    "priceX18": str(price_x18),
                    "amount": str(amount_x18),
                    "expiration": str(expiration),
                    "nonce": str(nonce),
                    "appendix": str(appendix)
                },
                "product_id": product_id,
                "signature": signature  # Already has 0x prefix from HexBytes.hex()
            }

            logger.info(f"Creating limit order: {symbol} {'BUY' if is_buy else 'SELL'} {amount} @ ${price}")

            response = await self._execute("place_order", payload)

            if response.get("status") == "success":
                logger.info(f"✅ Limit order created: {response}")
                return response
            else:
                logger.error(f"❌ Limit order failed: {response}")
                return response

        except Exception as e:
            logger.error(f"Error creating limit order: {e}")
            return {"status": "failure", "error": str(e)}

    async def cancel_order(self, product_id: int, order_digest: str) -> bool:
        """
        Cancel a single order

        Args:
            product_id: Product ID
            order_digest: Order digest/ID to cancel

        Returns:
            True if successful
        """
        return await self.cancel_orders([product_id], [order_digest])

    async def cancel_orders(self, product_ids: List[int], digests: List[str]) -> bool:
        """
        Cancel multiple orders

        Args:
            product_ids: List of product IDs
            digests: List of order digests to cancel

        Returns:
            True if successful
        """
        try:
            nonce = self._generate_nonce()

            # Sign the cancel request
            signature = self._sign_cancel(product_ids, digests, nonce)

            payload = {
                "tx": {
                    "sender": self._get_subaccount_bytes32(),
                    "productIds": product_ids,
                    "digests": digests,
                    "nonce": str(nonce)
                },
                "signature": signature  # Already has 0x prefix from HexBytes.hex()
            }

            response = await self._execute("cancel_orders", payload)

            if response.get("status") == "success":
                logger.info(f"✅ Orders cancelled")
                return True
            else:
                logger.error(f"❌ Cancel failed: {response}")
                return False

        except Exception as e:
            logger.error(f"Error cancelling orders: {e}")
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        """
        Cancel all open orders, optionally for a specific symbol
        """
        try:
            # Get all orders
            product_id = None
            if symbol:
                product = await self.get_product_by_symbol(symbol)
                if product:
                    product_id = product.get("product_id", product.get("id"))

            orders = await self.get_orders(product_id)

            if not orders:
                logger.info("No orders to cancel")
                return True

            # Group by product ID
            product_ids = []
            digests = []
            for order in orders:
                product_ids.append(order.get("product_id"))
                digests.append(order.get("digest", order.get("order_id")))

            return await self.cancel_orders(product_ids, digests)

        except Exception as e:
            logger.error(f"Error cancelling all orders: {e}")
            return False

    # ===== P&L and Analytics Methods =====

    async def _archive_query(self, payload: Dict) -> Dict:
        """
        Make a POST request to Nado Archive API for historical data
        """
        headers = {
            "Content-Type": "application/json",
            "Accept-Encoding": "gzip, deflate"
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.archive_url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        error_text = await resp.text()
                        logger.error(f"Archive API Error {resp.status}: {error_text[:200]}")
                        return {"error": error_text}
        except Exception as e:
            logger.error(f"Archive query failed: {e}")
            return {"error": str(e)}

    async def get_pnl(self, hours: int = 24) -> Dict:
        """
        Get P&L for a time period using ACTUAL EXCHANGE DATA from Archive API.

        This is the CORRECT way to calculate P&L - summing realized_pnl and fees
        from the matches (trade history) endpoint.

        Args:
            hours: Number of hours to look back (default: 24)

        Returns:
            Dict with realized_pnl, fees, net_pnl, trade_count
        """
        import time

        now = int(time.time())
        cutoff = now - (hours * 60 * 60)

        # Query matches from Archive API
        payload = {
            "matches": {
                "subaccounts": [self._get_subaccount_bytes32()],
                "limit": 500,
                "isolated": False
            }
        }

        response = await self._archive_query(payload)

        if "error" in response:
            return {"error": response["error"]}

        matches = response.get("matches", [])
        txs = response.get("txs", [])

        # Build timestamp lookup from txs
        tx_timestamps = {}
        for tx in txs:
            idx = tx.get("submission_idx")
            ts = int(tx.get("timestamp", 0))
            tx_timestamps[idx] = ts

        # Calculate P&L for period
        total_realized_pnl = 0.0
        total_fees = 0.0
        trade_count = 0

        for match in matches:
            idx = match.get("submission_idx")
            ts = tx_timestamps.get(idx, 0)

            if ts >= cutoff:
                realized_pnl = self._from_x18(match.get("realized_pnl", "0"))
                fee = self._from_x18(match.get("fee", "0"))

                total_realized_pnl += realized_pnl
                total_fees += fee
                trade_count += 1

        net_pnl = total_realized_pnl - total_fees

        return {
            "hours": hours,
            "trade_count": trade_count,
            "realized_pnl": total_realized_pnl,
            "fees": total_fees,
            "net_pnl": net_pnl
        }

    # ===== Utility Methods =====

    async def verify_linked_signer(self) -> bool:
        """
        Verify that the linked signer is properly configured
        """
        current_signer = await self.get_linked_signer()

        if not current_signer:
            logger.warning("No linked signer set for this subaccount")
            logger.warning("Please enable 1-Click Trading in Nado UI or use link_signer()")
            return False

        if current_signer.lower() != self.signer.address.lower():
            logger.error(f"Linked signer mismatch!")
            logger.error(f"  Expected: {self.signer.address}")
            logger.error(f"  Actual: {current_signer}")
            return False

        logger.info(f"✅ Linked signer verified: {current_signer}")
        return True


async def test_connection():
    """Test Nado SDK connection"""
    wallet_address = os.getenv("NADO_WALLET_ADDRESS")
    linked_signer_key = os.getenv("NADO_LINKED_SIGNER_PRIVATE_KEY")
    subaccount_name = os.getenv("NADO_SUBACCOUNT_NAME", "default")
    testnet = os.getenv("NADO_TESTNET", "false").lower() == "true"

    if not wallet_address or not linked_signer_key:
        print("❌ Missing NADO_WALLET_ADDRESS or NADO_LINKED_SIGNER_PRIVATE_KEY in .env")
        print("\nSetup instructions:")
        print("1. Add your wallet address to .env: NADO_WALLET_ADDRESS=0x...")
        print("2. Generate a random private key for linked signer")
        print("3. Enable 1-Click Trading in Nado UI with the linked signer address")
        print("4. Add the linked signer private key to .env: NADO_LINKED_SIGNER_PRIVATE_KEY=0x...")
        return

    print(f"Testing Nado connection ({'testnet' if testnet else 'mainnet'})...")
    print(f"Wallet: {wallet_address[:10]}...")

    sdk = NadoSDK(
        wallet_address=wallet_address,
        linked_signer_private_key=linked_signer_key,
        subaccount_name=subaccount_name,
        testnet=testnet
    )

    # Test 1: Get products
    print("\n1️⃣ Testing get_products()...")
    products = await sdk.get_products()
    if products:
        print(f"✅ Found {len(products)} products")
        for p in products[:5]:
            print(f"   - {p.get('symbol', 'unknown')}")
    else:
        print("❌ Failed to get products")

    # Test 2: Verify linked signer
    print("\n2️⃣ Verifying linked signer...")
    verified = await sdk.verify_linked_signer()
    if not verified:
        print("⚠️  Linked signer not configured - trading will fail")
        print("   Enable 1-Click Trading in Nado UI first!")

    # Test 3: Get balance
    print("\n3️⃣ Testing get_balance()...")
    balance = await sdk.get_balance()
    if balance is not None:
        print(f"✅ Balance: ${balance:.2f} USDT0")
    else:
        print("❌ Failed to get balance (subaccount may not exist)")

    # Test 4: Get positions
    print("\n4️⃣ Testing get_positions()...")
    positions = await sdk.get_positions()
    print(f"✅ Open positions: {len(positions)}")
    for pos in positions:
        print(f"   - {pos}")

    # Test 5: Get orders
    print("\n5️⃣ Testing get_orders()...")
    orders = await sdk.get_orders()
    print(f"✅ Open orders: {len(orders)}")

    print("\n✅ Connection test complete!")


if __name__ == "__main__":
    asyncio.run(test_connection())
