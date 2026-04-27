"""
Exchange Adapter - Unified interface for Hibachi, Nado, Extended.

Thin wrappers over existing SDKs in dexes/. No business logic.
Each adapter normalizes the SDK interface to a common async API.
"""

import asyncio
import logging
import math
import os
import time
from abc import ABC, abstractmethod
from typing import Optional, List, Dict
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


class ExchangeAdapter(ABC):
    """Abstract base for exchange operations needed by momentum bot."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Exchange name for logging."""

    @property
    @abstractmethod
    def supports_post_only(self) -> bool:
        """Whether this exchange supports POST_ONLY limit orders."""

    @abstractmethod
    async def get_equity(self) -> float:
        """Get total account equity (balance + unrealized PnL) from exchange API."""

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[dict]:
        """
        Get current position for symbol.

        Returns:
            dict with {size: float, side: "LONG"/"SHORT", entry_price: float}
            or None if no position.
        """

    @abstractmethod
    async def place_limit(
        self, symbol: str, side: str, price: float, size: float
    ) -> Optional[str]:
        """
        Place a limit order. Uses POST_ONLY where supported.

        Args:
            symbol: Market symbol (exchange-specific format)
            side: "BUY" or "SELL"
            price: Limit price
            size: Size in base currency

        Returns:
            Order ID string, or None on failure.
        """

    @abstractmethod
    async def cancel_all(self, symbol: str) -> int:
        """Cancel all open orders for symbol. Returns count cancelled."""

    @abstractmethod
    async def get_open_orders(self, symbol: str) -> List[dict]:
        """Get open orders for symbol."""

    @abstractmethod
    async def get_price(self, symbol: str) -> Optional[float]:
        """Get current mark/last price."""

    @abstractmethod
    async def close_position(self, symbol: str) -> bool:
        """Close entire position via market order. Returns success."""

    async def get_all_positions(self) -> List[dict]:
        """
        Get ALL open positions on this exchange.

        Returns list of dicts:
            {symbol: str, side: "LONG"/"SHORT", size: float, entry_price: float, notional: float}
        """
        return []

    async def discover_markets(self) -> List[dict]:
        """
        Discover all tradeable markets on this exchange.

        Returns list of dicts:
            {asset: str, symbol: str, min_notional: float}
        """
        return []


# ─── Hibachi ──────────────────────────────────────────────────────

class HibachiAdapter(ExchangeAdapter):
    """
    Wraps dexes/hibachi/hibachi_sdk.py.

    No POST_ONLY support - use wider offsets (15-20 bps).
    Equity via get_account_info().
    """

    def __init__(self):
        from dexes.hibachi.hibachi_sdk import HibachiSDK

        api_key = os.getenv("HIBACHI_PUBLIC_KEY")
        api_secret = os.getenv("HIBACHI_PRIVATE_KEY")
        account_id = os.getenv("HIBACHI_ACCOUNT_ID")

        if not all([api_key, api_secret, account_id]):
            raise ValueError(
                "Missing Hibachi credentials. Set in .env: "
                "HIBACHI_PUBLIC_KEY, HIBACHI_PRIVATE_KEY, HIBACHI_ACCOUNT_ID"
            )

        self.sdk = HibachiSDK(api_key, api_secret, account_id)

    @property
    def name(self) -> str:
        return "hibachi"

    @property
    def supports_post_only(self) -> bool:
        return False

    async def get_equity(self) -> float:
        # BUG FIX (Apr 13): API intermittently returns HTML instead of JSON,
        # causing get_account_info() to return None → equity=0.0 in logs.
        # Add retries and log warnings.
        try:
            info = await self.sdk.get_account_info()
            if info:
                equity = info.get("equity") or info.get("balance") or info.get("availableBalance")
                if equity is not None:
                    return float(equity)
        except Exception as e:
            logger.warning(f"[hibachi] get_account_info failed: {e}")

        try:
            bal = await self.sdk.get_balance()
            if bal and float(bal) > 0:
                return float(bal)
        except Exception as e:
            logger.warning(f"[hibachi] get_balance failed: {e}")

        # Return -1 to signal failure (caller should handle)
        # Don't return 0.0 silently — that's what caused phantom $0 equity logs
        logger.error("[hibachi] Could not get equity from any source")
        return -1.0

    async def get_position(self, symbol: str) -> Optional[dict]:
        positions = await self.sdk.get_positions()
        for p in positions:
            if p.get("symbol") == symbol:
                qty = float(p.get("quantity", p.get("size", 0)))
                direction = p.get("direction", "Long")
                side = "SHORT" if direction == "Short" else "LONG"
                if qty == 0:
                    continue
                return {
                    "size": abs(qty),
                    "side": side,
                    "entry_price": float(p.get("openPrice", p.get("entryPrice", p.get("entry_price", 0)))),
                }
        return None

    async def place_limit(
        self, symbol: str, side: str, price: float, size: float
    ) -> Optional[str]:
        is_buy = side == "BUY"
        result = await self.sdk.create_limit_order(
            symbol=symbol,
            is_buy=is_buy,
            amount=size,
            price=price,
        )
        if result and "error" not in result:
            order_id = result.get("orderId") or result.get("order_id") or str(time.time())
            return str(order_id)
        logger.warning(f"[hibachi] Limit order failed: {result}")
        return None

    async def cancel_all(self, symbol: str) -> int:
        return await self.sdk.cancel_all_orders(symbol)

    async def get_open_orders(self, symbol: str) -> List[dict]:
        return await self.sdk.get_orders(symbol)

    async def get_price(self, symbol: str) -> Optional[float]:
        return await self.sdk.get_price(symbol)

    async def close_position(self, symbol: str) -> bool:
        """Maker-only close on Hibachi. Tries progressively wider limits;
        gives up and returns False if none fill. Bot retries next cycle.

        NEVER falls through to market order. User directive 2026-04-20:
        taker fees bleed the account; accept longer time-to-exit instead.
        """
        pos = await self.get_position(symbol)
        if not pos:
            return True

        is_buy = pos["side"] == "SHORT"
        price = await self.sdk.get_price(symbol)
        if not price:
            logger.warning(f"[hibachi] No mark price for {symbol}, skip close cycle")
            return False

        # Widening ladder: 2bps → 5bps → 10bps → 20bps. Total wait ≤ ~40s.
        for offset_bps in (2, 5, 10, 20):
            # BUY close: place BELOW mid to be a maker
            # SELL close: place ABOVE mid to be a maker
            mult = (1 - offset_bps / 10_000) if is_buy else (1 + offset_bps / 10_000)
            limit_price = round(price * mult, 4)

            logger.info(
                f"[hibachi] Maker close try ({offset_bps}bps): "
                f"{'BUY' if is_buy else 'SELL'} {symbol} @ ${limit_price:.4f} (mark=${price:.4f})"
            )

            try:
                result = await self.sdk.create_limit_order(
                    symbol=symbol, is_buy=is_buy,
                    amount=pos["size"], price=limit_price,
                )
            except Exception as e:
                logger.warning(f"[hibachi] Limit placement raised: {e}")
                continue
            if not result or "error" in result:
                continue

            # Wait for fill (6s per try = 10 x 600ms)
            for _ in range(6):
                await asyncio.sleep(1)
                check = await self.get_position(symbol)
                if not check:
                    logger.info(f"[hibachi] Maker close FILLED for {symbol} @ {offset_bps}bps")
                    return True
                pos = check  # in case size changed

            # Not filled at this offset — cancel and widen
            try:
                await self.sdk.cancel_all_orders(symbol)
            except Exception:
                pass
            await asyncio.sleep(0.5)
            price = await self.sdk.get_price(symbol) or price

        logger.info(f"[hibachi] Maker close gave up after 4 widenings — will retry next cycle")
        return False

    async def get_all_positions(self) -> List[dict]:
        positions = await self.sdk.get_positions()
        results = []
        for p in positions:
            qty = float(p.get("quantity", p.get("size", 0)))
            if qty == 0:
                continue
            direction = p.get("direction", "Long")
            side = "SHORT" if direction == "Short" else "LONG"
            entry = float(p.get("openPrice", p.get("entryPrice", p.get("entry_price", 0))))
            symbol = p.get("symbol", "?")
            upnl = float(p.get("unrealizedTradingPnl", p.get("unrealizedPnl", 0)))
            notional = float(p.get("notionalValue", 0)) or abs(qty) * entry
            results.append({
                "symbol": symbol, "side": side, "size": abs(qty),
                "entry_price": entry, "notional": notional,
                "unrealized_pnl": upnl,
            })
        return results

    async def discover_markets(self) -> List[dict]:
        markets = await self.sdk.get_markets()
        results = []
        for m in markets:
            if m.get("status") != "LIVE":
                continue
            asset = m.get("underlyingSymbol", "")
            symbol = m.get("symbol", "")
            if asset and symbol:
                results.append({
                    "asset": asset,
                    "symbol": symbol,
                    "min_notional": float(m.get("minNotional", 1)),
                })
        return results


# ─── Nado ─────────────────────────────────────────────────────────

class NadoAdapter(ExchangeAdapter):
    """
    Wraps dexes/nado/nado_sdk.py.

    POST_ONLY via order_type="POST_ONLY".
    get_balance() returns spot only (not equity).
    Has get_pnl() via Archive API.
    """

    def __init__(self):
        from dexes.nado.nado_sdk import NadoSDK

        wallet = os.getenv("NADO_WALLET_ADDRESS")
        signer_key = os.getenv("NADO_LINKED_SIGNER_PRIVATE_KEY")
        subaccount = os.getenv("NADO_SUBACCOUNT_NAME", "default")

        if not all([wallet, signer_key]):
            raise ValueError(
                "Missing Nado credentials. Set in .env: "
                "NADO_WALLET_ADDRESS, NADO_LINKED_SIGNER_PRIVATE_KEY"
            )

        self.sdk = NadoSDK(wallet, signer_key, subaccount)
        # Safety valve: if maker-only close keeps failing on a symbol for
        # >STUCK_GRACE_MIN, allow ONE market close to unstick. Prevents the
        # pattern where a position rides far past SL because the book is too
        # thin to find a maker. Reset on successful close.
        self._stuck_first_attempt_ts: Dict[str, float] = {}
        self.STUCK_GRACE_MIN = 15.0

    @property
    def name(self) -> str:
        return "nado"

    @property
    def supports_post_only(self) -> bool:
        return True

    async def get_equity(self) -> float:
        # Use Nado's PnL health: net equity = assets - liabilities
        # BUG FIX (Apr 13): Was returning assets only, not subtracting liabilities.
        # This caused the bot to think equity was ~$40 when it was actually ~$12,
        # leading to massively oversized positions.
        info = await self.sdk.get_subaccount_info()
        if info:
            healths = info.get("healths", [])
            if len(healths) >= 3:
                assets_x18 = int(healths[2].get("assets", "0"))
                liabilities_x18 = int(healths[2].get("liabilities", "0"))
                net_equity = self.sdk._from_x18(assets_x18) - self.sdk._from_x18(liabilities_x18)
                return max(net_equity, 0.0)  # Don't return negative equity

        # Fallback: spot balance only (no position PnL)
        balance = await self.sdk.get_balance()
        return float(balance) if balance else 0.0

    async def get_position(self, symbol: str) -> Optional[dict]:
        positions = await self.sdk.get_positions()
        for p in positions:
            if p.get("symbol") == symbol:
                amount = p.get("amount_float", 0.0)
                if amount == 0:
                    continue
                side = "LONG" if amount > 0 else "SHORT"
                # Nado doesn't directly expose entry_price in position data
                # Use v_quote_balance for approximate entry tracking
                return {
                    "size": abs(amount),
                    "side": side,
                    "entry_price": 0.0,  # Must track externally
                }
        return None

    async def place_limit(
        self, symbol: str, side: str, price: float, size: float
    ) -> Optional[str]:
        import math
        is_buy = side == "BUY"

        # Fetch dynamic price/size increments from product info
        product = await self.sdk.get_product_by_symbol(symbol)
        if not product:
            logger.warning(f"[nado] Unknown symbol: {symbol}")
            return None

        price_inc = product.get("price_increment", 1.0)
        size_inc = product.get("size_increment", 0.00005)
        oracle_price = product.get("oracle_price", price)

        # Round price to increment (round to kill floating point artifacts)
        if is_buy:
            price = round(math.floor(price / price_inc) * price_inc, 10)
            # POST_ONLY: ensure limit is at least 1 tick below oracle
            if price >= oracle_price:
                price = round(math.floor(oracle_price / price_inc) * price_inc - price_inc, 10)
        else:
            price = round(math.ceil(price / price_inc) * price_inc, 10)
            # POST_ONLY: ensure limit is at least 1 tick above oracle
            if price <= oracle_price:
                price = round(math.ceil(oracle_price / price_inc) * price_inc + price_inc, 10)

        # Round size to increment (round to kill floating point artifacts)
        size = round(math.floor(size / size_inc) * size_inc, 10)
        # If flooring dropped notional below on-chain minimum ($100), round up instead
        min_notional = product.get("min_notional", 100.0)
        if size * price < min_notional and size > 0:
            size = round(math.ceil((size + 1e-12) / size_inc) * size_inc, 10)
        if size <= 0:
            logger.warning(f"[nado] Size too small after rounding: {size} (inc={size_inc})")
            return None

        result = await self.sdk.create_limit_order(
            symbol=symbol,
            is_buy=is_buy,
            amount=size,
            price=price,
            order_type="POST_ONLY",
        )
        if result and result.get("status") == "success":
            digest = result.get("data", {}).get("digest", "")
            return digest or str(time.time())
        logger.warning(f"[nado] Limit order failed: {result}")
        return None

    async def cancel_all(self, symbol: str) -> int:
        success = await self.sdk.cancel_all_orders(symbol)
        return 1 if success else 0

    async def get_open_orders(self, symbol: str) -> List[dict]:
        product = await self.sdk.get_product_by_symbol(symbol)
        if not product:
            return []
        product_id = product.get("product_id", product.get("id"))
        return await self.sdk.get_orders(product_id)

    async def get_price(self, symbol: str) -> Optional[float]:
        # Nado doesn't have a direct get_price - use product oracle
        product = await self.sdk.get_product_by_symbol(symbol)
        if product:
            oracle = product.get("oracle_price")
            if oracle:
                return float(oracle)
        return None

    async def close_position(self, symbol: str) -> bool:
        import time as _time
        pos = await self.get_position(symbol)
        if not pos:
            self._stuck_first_attempt_ts.pop(symbol, None)
            return True
        is_buy = pos["side"] == "SHORT"

        # Safety valve: record when this close started retrying.
        now = _time.time()
        if symbol not in self._stuck_first_attempt_ts:
            self._stuck_first_attempt_ts[symbol] = now
        stuck_minutes = (now - self._stuck_first_attempt_ts[symbol]) / 60.0

        # ── Step 1: Try POST_ONLY maker close (0% fee) ──
        try:
            product = await self.sdk.get_product_by_symbol(symbol)
            if product:
                oracle_price = product.get("oracle_price", 0)
                price_inc = product.get("price_increment", 1.0)
                size_inc = product.get("size_increment", 0.00005)

                if oracle_price and price_inc:
                    # Place at oracle — this should rest on book as maker
                    if is_buy:
                        # Closing short: buy at oracle (bid side)
                        maker_price = round(math.floor(oracle_price / price_inc) * price_inc, 10)
                    else:
                        # Closing long: sell at oracle (ask side)
                        maker_price = round(math.ceil(oracle_price / price_inc) * price_inc, 10)

                    size = round(math.floor(pos["size"] / size_inc) * size_inc, 10)

                    logger.info(
                        f"[nado] Trying maker close: {'BUY' if is_buy else 'SELL'} "
                        f"{symbol} @ ${maker_price:.4f} (oracle={oracle_price:.4f})"
                    )

                    result = await self.sdk.create_limit_order(
                        symbol=symbol,
                        is_buy=is_buy,
                        amount=size,
                        price=maker_price,
                        order_type="POST_ONLY",
                    )

                    if result and result.get("status") == "success":
                        # Wait for fill (check every 2s, up to 12s)
                        for _ in range(6):
                            await asyncio.sleep(2)
                            check = await self.get_position(symbol)
                            if not check or check.get("size", 0) == 0:
                                logger.info(f"[nado] Maker close FILLED for {symbol}")
                                return True

                        # Not filled — cancel
                        logger.info(f"[nado] Maker close not filled for {symbol}, falling back to market")
                        await self.cancel_all(symbol)
                        await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"[nado] Maker close attempt failed: {e}")

        # Maker-only policy (user directive 2026-04-20): no market fallback.
        # Widen the POST_ONLY a few times, then give up and retry next cycle.
        for extra_bps in (5, 15, 30):
            try:
                product = await self.sdk.get_product_by_symbol(symbol)
                if not product:
                    break
                oracle_price = product.get("oracle_price", 0) or 0
                price_inc = product.get("price_increment", 1.0) or 1.0
                if not oracle_price or not price_inc:
                    break

                mult = (1 - extra_bps / 10_000) if is_buy else (1 + extra_bps / 10_000)
                wider_price = oracle_price * mult
                if is_buy:
                    wider_price = round(math.floor(wider_price / price_inc) * price_inc, 10)
                else:
                    wider_price = round(math.ceil(wider_price / price_inc) * price_inc, 10)

                logger.info(
                    f"[nado] Maker close wider try ({extra_bps}bps): "
                    f"{'BUY' if is_buy else 'SELL'} {symbol} @ ${wider_price:.4f} "
                    f"(oracle=${oracle_price:.4f})"
                )

                pos = await self.get_position(symbol)
                if not pos:
                    return True
                size = round(math.floor(pos["size"] / (product.get("size_increment", 0.00005) or 0.00005))
                             * (product.get("size_increment", 0.00005) or 0.00005), 10)

                result = await self.sdk.create_limit_order(
                    symbol=symbol, is_buy=is_buy, amount=size,
                    price=wider_price, order_type="POST_ONLY",
                )
                if result and result.get("status") == "success":
                    for _ in range(6):
                        await asyncio.sleep(1)
                        check = await self.get_position(symbol)
                        if not check or check.get("size", 0) == 0:
                            logger.info(f"[nado] Maker close FILLED wider @ {extra_bps}bps")
                            return True
                    await self.cancel_all(symbol)
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"[nado] Wider maker close attempt failed: {e}")

        # Safety valve: if we've been retrying maker-only for >STUCK_GRACE_MIN
        # minutes on the same position, allow one market order to unstick it.
        # Prevents positions riding deep past SL when book is too thin for makers.
        if stuck_minutes > self.STUCK_GRACE_MIN:
            logger.warning(
                f"[nado] STUCK SAFETY VALVE: maker-only failed for {stuck_minutes:.1f}min "
                f"on {symbol}. Allowing one market close."
            )
            try:
                pos_now = await self.get_position(symbol)
                if pos_now and pos_now.get("size", 0) > 0:
                    result = await self.sdk.create_market_order(
                        symbol=symbol, is_buy=is_buy,
                        amount=pos_now["size"], reduce_only=True,
                    )
                    if result and result.get("status") == "success":
                        self._stuck_first_attempt_ts.pop(symbol, None)
                        return True
            except Exception as e:
                logger.warning(f"[nado] Safety-valve market close failed: {e}")

        logger.info(f"[nado] Maker close gave up — will retry next cycle (stuck {stuck_minutes:.1f}min)")
        return False

    async def get_all_positions(self) -> List[dict]:
        positions = await self.sdk.get_positions()
        results = []
        for p in positions:
            amount = p.get("amount_float", 0.0)
            if amount == 0:
                continue
            symbol = p.get("symbol", "?")
            side = "LONG" if amount > 0 else "SHORT"
            # Get oracle price for notional estimate
            oracle = await self.get_price(symbol)
            notional = abs(amount) * oracle if oracle else 0
            results.append({
                "symbol": symbol, "side": side, "size": abs(amount),
                "entry_price": 0.0, "notional": notional,
                "unrealized_pnl": 0.0,  # Nado doesn't expose per-position PnL
            })
        return results

    async def discover_markets(self) -> List[dict]:
        products = await self.sdk.get_products()
        results = []
        for p in products:
            symbol = p.get("symbol", "")
            if not symbol or not symbol.endswith("-PERP"):
                continue
            asset = symbol.replace("-PERP", "")
            results.append({
                "asset": asset,
                "symbol": symbol,
                "min_notional": 100.0,  # Nado always $100 min
            })
        return results


# ─── Extended ─────────────────────────────────────────────────────

class ExtendedAdapter(ExchangeAdapter):
    """
    Wraps dexes/extended/extended_sdk.py + x10 PerpetualTradingClient.

    POST_ONLY supported. Uses x10 client for order placement.
    get_balance() returns dict with equity.
    """

    def __init__(self):
        from x10.perpetual.accounts import StarkPerpetualAccount
        from x10.perpetual.configuration import MAINNET_CONFIG
        from x10.perpetual.trading_client import PerpetualTradingClient

        api_key = os.getenv("EXTENDED") or os.getenv("EXTENDED_API_KEY")
        private_key = os.getenv("EXTENDED_STARK_PRIVATE_KEY")
        public_key = os.getenv("EXTENDED_STARK_PUBLIC_KEY")
        vault = os.getenv("EXTENDED_VAULT")

        if not all([api_key, private_key, public_key, vault]):
            raise ValueError(
                "Missing Extended credentials. Set in .env: "
                "EXTENDED_API_KEY, EXTENDED_STARK_PRIVATE_KEY, "
                "EXTENDED_STARK_PUBLIC_KEY, EXTENDED_VAULT"
            )

        stark_account = StarkPerpetualAccount(
            vault=int(vault),
            private_key=private_key,
            public_key=public_key,
            api_key=api_key,
        )

        self.client = PerpetualTradingClient(
            endpoint_config=MAINNET_CONFIG,
            stark_account=stark_account,
        )

        # Symbol format mapping (populated dynamically by discover_markets)
        self._symbol_map = {
            "BTC": "BTC-USD",
            "ETH": "ETH-USD",
            "SOL": "SOL-USD",
        }

        # Size increment per asset (populated by discover_markets)
        self._size_inc = {
            "BTC": 0.00001, "ETH": 0.001, "SOL": 0.01,
        }

        # Price tick size per asset (populated by discover_markets)
        self._price_tick = {
            "BTC": 1.0, "ETH": 0.1, "SOL": 0.01,
        }

    @property
    def name(self) -> str:
        return "extended"

    @property
    def supports_post_only(self) -> bool:
        return True

    def _to_market(self, symbol: str) -> str:
        """Convert short symbol to Extended market name."""
        return self._symbol_map.get(symbol, f"{symbol}-USD")

    @staticmethod
    def _to_decimal(value: float, increment: float) -> Decimal:
        """Format value as Decimal with precision matching the increment."""
        if increment >= 1.0:
            return Decimal(str(int(value)))
        decimals = max(0, -int(math.floor(math.log10(increment))))
        return Decimal(f"{value:.{decimals}f}")

    async def get_equity(self) -> float:
        try:
            balance = await self.client.account.get_balance()
            if balance and balance.data:
                return float(balance.data.equity)
        except Exception as e:
            logger.error(f"[extended] Error getting equity: {e}")
        return 0.0

    async def get_position(self, symbol: str) -> Optional[dict]:
        market = self._to_market(symbol)
        try:
            positions = await self.client.account.get_positions(
                market_names=[market]
            )
            if positions and positions.data:
                for pos in positions.data:
                    size = float(pos.size) if pos.size else 0
                    if abs(size) > 0:
                        entry = float(pos.open_price) if pos.open_price else 0
                        side = str(pos.side).upper()
                        return {
                            "size": abs(size),
                            "side": "SHORT" if side == "SHORT" else "LONG",
                            "entry_price": entry,
                        }
        except Exception as e:
            logger.error(f"[extended] Error getting position: {e}")
        return None

    async def place_limit(
        self, symbol: str, side: str, price: float, size: float
    ) -> Optional[str]:
        from x10.perpetual.orders import OrderSide, TimeInForce

        market = self._to_market(symbol)
        order_side = OrderSide.BUY if side == "BUY" else OrderSide.SELL

        asset = symbol.split("-")[0] if "-" in symbol else symbol

        # Round size to increment (e.g., 10 for ENA, 0.001 for ETH)
        size_inc = self._size_inc.get(asset, 0.00001)
        size = math.floor(size / size_inc) * size_inc
        size = round(size, 10)  # remove float artifacts

        # Round price to tick (e.g., 0.0001 for sub-dollar, 1.0 for BTC)
        tick = self._price_tick.get(asset, 1.0)
        price = round(round(price / tick) * tick, 10)

        # Format Decimals with exact precision matching the increment
        size_decimal = self._to_decimal(size, size_inc)
        price_decimal = self._to_decimal(price, tick)

        try:
            order = await self.client.place_order(
                market_name=market,
                amount_of_synthetic=size_decimal,
                price=price_decimal,
                side=order_side,
                post_only=True,
                time_in_force=TimeInForce.GTT,
                expire_time=datetime.now(timezone.utc) + timedelta(hours=1),
            )
            if order and order.data:
                return str(order.data.id)
        except Exception as e:
            logger.warning(f"[extended] Limit order failed: {e}")
        return None

    async def cancel_all(self, symbol: str) -> int:
        market = self._to_market(symbol)
        try:
            await self.client.orders.mass_cancel(markets=[market])
            return 1
        except Exception as e:
            logger.error(f"[extended] Cancel failed: {e}")
        return 0

    async def get_open_orders(self, symbol: str) -> List[dict]:
        market = self._to_market(symbol)
        try:
            orders = await self.client.account.get_open_orders(
                market_names=[market]
            )
            if orders and orders.data:
                return [{"id": str(o.id), "side": str(o.side), "price": str(o.price)} for o in orders.data]
        except Exception as e:
            logger.error(f"[extended] Error getting orders: {e}")
        return []

    async def get_price(self, symbol: str) -> Optional[float]:
        market = self._to_market(symbol)
        try:
            # Try orderbook mid first
            ob = await self.client.markets_info.get_orderbook_snapshot(
                market_name=market
            )
            if ob and ob.data:
                bid_list = ob.data.bid
                ask_list = ob.data.ask
                if bid_list and ask_list:
                    best_bid = float(bid_list[0].price)
                    best_ask = float(ask_list[0].price)
                    return (best_bid + best_ask) / 2.0

            # Fallback to market stats
            stats = await self.client.markets_info.get_market_statistics(
                market_name=market
            )
            if stats and stats.data:
                return float(stats.data.last_price)
        except Exception as e:
            logger.error(f"[extended] Error getting price: {e}")
        return None

    async def close_position(self, symbol: str) -> bool:
        from x10.perpetual.orders import OrderSide, TimeInForce

        pos = await self.get_position(symbol)
        if not pos:
            return True

        market = self._to_market(symbol)
        order_side = OrderSide.BUY if pos["side"] == "SHORT" else OrderSide.SELL
        asset = symbol.split("-")[0] if "-" in symbol else symbol
        tick = self._price_tick.get(asset, 1.0)
        size_inc = self._size_inc.get(asset, 0.00001)
        size_decimal = self._to_decimal(pos["size"], size_inc)

        try:
            # ── Step 1: Try POST_ONLY maker close (0% fee) ──
            ob = await self.client.markets_info.get_orderbook_snapshot(
                market_name=market
            )
            if ob and ob.data and ob.data.bid and ob.data.ask:
                best_bid = float(ob.data.bid[0].price)
                best_ask = float(ob.data.ask[0].price)

                # Place at best price where we'd be maker
                if order_side == OrderSide.SELL:
                    # Closing long: sell at best ask (join offer side)
                    maker_price = best_ask
                else:
                    # Closing short: buy at best bid (join bid side)
                    maker_price = best_bid

                maker_price = round(round(maker_price / tick) * tick, 10)
                maker_price_decimal = self._to_decimal(maker_price, tick)

                logger.info(
                    f"[extended] Trying maker close: {order_side.name} {symbol} "
                    f"@ ${maker_price:.4f} (bid={best_bid:.4f} ask={best_ask:.4f})"
                )

                maker_order = await self.client.place_order(
                    market_name=market,
                    amount_of_synthetic=size_decimal,
                    price=maker_price_decimal,
                    side=order_side,
                    post_only=True,
                    reduce_only=True,
                    time_in_force=TimeInForce.GTT,
                    expire_time=datetime.now(timezone.utc) + timedelta(minutes=1),
                )

                if maker_order and maker_order.data:
                    # Wait for fill (check every 2s, up to 12s)
                    for _ in range(6):
                        await asyncio.sleep(2)
                        check = await self.get_position(symbol)
                        if not check or check["size"] == 0:
                            logger.info(f"[extended] Maker close FILLED for {symbol}")
                            return True

                    # Not filled — cancel maker order
                    logger.info(f"[extended] Maker close not filled for {symbol}, falling back to IOC")
                    await self.cancel_all(symbol)
                    await asyncio.sleep(1)

            # ── Step 2: Fallback to IOC taker close ──
            price = await self.get_price(symbol)
            if not price:
                return False

            if order_side == OrderSide.BUY:
                exec_price = price * 1.005
            else:
                exec_price = price * 0.995

            exec_price = round(round(exec_price / tick) * tick, 10)
            price_decimal = self._to_decimal(exec_price, tick)

            order = await self.client.place_order(
                market_name=market,
                amount_of_synthetic=size_decimal,
                price=price_decimal,
                side=order_side,
                post_only=False,
                reduce_only=True,
                time_in_force=TimeInForce.IOC,
                expire_time=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            return bool(order and order.data)
        except Exception as e:
            logger.error(f"[extended] Close position failed: {e}")
        return False

    async def get_all_positions(self) -> List[dict]:
        try:
            positions = await self.client.account.get_positions()
            results = []
            if positions and positions.data:
                for pos in positions.data:
                    size = float(pos.size) if pos.size else 0
                    if abs(size) == 0:
                        continue
                    entry = float(pos.open_price) if pos.open_price else 0
                    side = str(pos.side).upper()
                    side = "SHORT" if side == "SHORT" else "LONG"
                    upnl = float(pos.unrealized_pnl) if hasattr(pos, 'unrealized_pnl') and pos.unrealized_pnl else 0
                    market = str(pos.market) if hasattr(pos, 'market') else "?"
                    # Return asset name (e.g. "WIF") not market name (e.g. "WIF-USD")
                    # so it's compatible with close_position() and other adapter methods
                    asset = market.replace("-USD", "") if market.endswith("-USD") else market
                    results.append({
                        "symbol": asset, "side": side, "size": abs(size),
                        "entry_price": entry, "notional": abs(size) * entry,
                        "unrealized_pnl": upnl,
                    })
            return results
        except Exception as e:
            logger.error(f"[extended] Error getting all positions: {e}")
            return []

    async def discover_markets(self) -> List[dict]:
        """Discover all active markets on Extended via x10 SDK.

        Returns symbol=asset_name (e.g. "BTC") because adapter methods
        internally convert via _to_market() to "BTC-USD".
        """
        try:
            result = await self.client.markets_info.get_markets()
            markets = result.data if hasattr(result, "data") else result
            if not markets:
                return []

            results = []
            for m in markets:
                if not m.active:
                    continue

                asset = m.asset_name      # e.g. "BTC"
                market_name = m.name      # e.g. "BTC-USD"

                # Register in symbol map for _to_market()
                self._symbol_map[asset] = market_name

                min_notional = 10.0  # Extended default
                if m.trading_config:
                    min_notional = float(m.trading_config.min_order_size)
                    # Store increments for proper rounding
                    self._size_inc[asset] = float(m.trading_config.min_order_size_change)
                    self._price_tick[asset] = float(m.trading_config.min_price_change)

                # symbol = asset name (adapter converts internally)
                results.append({
                    "asset": asset,
                    "symbol": asset,
                    "min_notional": min_notional,
                })

            return results

        except Exception as e:
            logger.error(f"[extended] Error discovering markets: {e}")
            return []


# ─── Paradex ──────────────────────────────────────────────────────

class ParadexAdapter(ExchangeAdapter):
    """Wraps ParadexSubkey from paradex_py.

    Paradex pays MAKER REBATES (-0.5 bps) and charges 2 bps taker.
    Always prefer POST_ONLY. Maker-close pattern mirrors Nado's wider-limit
    safety valve.

    Python 3.11+ required (paradex_py SDK incompatible with 3.9).

    Symbol format on Paradex: f"{asset}-USD-PERP" (e.g. "BTC-USD-PERP").
    """

    STUCK_GRACE_MIN = 15.0
    _PRICE_CACHE_TTL = 2.0

    def __init__(self):
        from paradex_py import ParadexSubkey
        l2_address = os.getenv("PARADEX_ACCOUNT_ADDRESS")
        l2_private_key = os.getenv("PARADEX_PRIVATE_SUBKEY")
        if not all([l2_address, l2_private_key]):
            raise ValueError(
                "Missing Paradex credentials. Set in .env: "
                "PARADEX_ACCOUNT_ADDRESS, PARADEX_PRIVATE_SUBKEY"
            )
        self.client = ParadexSubkey(
            env="prod",
            l2_address=l2_address,
            l2_private_key=l2_private_key,
        )
        self._stuck_first_attempt_ts: Dict[str, float] = {}
        self._market_meta_cache: Dict[str, dict] = {}
        self._price_cache: Dict[str, tuple] = {}  # symbol -> (price, ts)

    @property
    def name(self) -> str:
        return "paradex"

    @property
    def supports_post_only(self) -> bool:
        return True

    def _market_for(self, asset: str) -> str:
        if asset.endswith("-USD-PERP"):
            return asset
        return f"{asset}-USD-PERP"

    async def get_equity(self) -> float:
        try:
            summary = self.client.api_client.fetch_account_summary()
            v = getattr(summary, "account_value", None)
            return float(v) if v is not None else 0.0
        except Exception as e:
            logger.error(f"[paradex] get_equity error: {e}")
            return 0.0

    async def get_position(self, symbol: str) -> Optional[dict]:
        market = self._market_for(symbol)
        try:
            resp = self.client.api_client.fetch_positions()
            for p in (resp.get("results") if isinstance(resp, dict) else []) or []:
                if p.get("market") != market:
                    continue
                if p.get("status") != "OPEN":
                    continue
                size = float(p.get("size", 0) or 0)
                if size == 0:
                    continue
                side = "LONG" if size > 0 else "SHORT"
                return {
                    "symbol": symbol,
                    "side": side,
                    "size": abs(size),
                    "entry_price": float(p.get("average_entry_price", 0) or 0),
                    "unrealized_pnl": float(p.get("unrealized_pnl", 0) or 0),
                }
        except Exception as e:
            logger.error(f"[paradex] get_position {symbol}: {e}")
        return None

    async def get_all_positions(self) -> List[dict]:
        try:
            resp = self.client.api_client.fetch_positions()
            out = []
            for p in (resp.get("results") if isinstance(resp, dict) else []) or []:
                if p.get("status") != "OPEN":
                    continue
                size = float(p.get("size", 0) or 0)
                if size == 0:
                    continue
                market = p.get("market", "")
                asset = market.replace("-USD-PERP", "")
                side = "LONG" if size > 0 else "SHORT"
                entry = float(p.get("average_entry_price", 0) or 0)
                out.append({
                    "symbol": asset,
                    "side": side,
                    "size": abs(size),
                    "entry_price": entry,
                    "notional": abs(size) * entry,
                    "unrealized_pnl": float(p.get("unrealized_pnl", 0) or 0),
                })
            return out
        except Exception as e:
            logger.error(f"[paradex] get_all_positions: {e}")
            return []

    async def get_price(self, symbol: str) -> Optional[float]:
        market = self._market_for(symbol)
        cached = self._price_cache.get(market)
        if cached and (time.time() - cached[1]) < self._PRICE_CACHE_TTL:
            return cached[0]
        try:
            bbo = self.client.api_client.fetch_bbo(market)
            if not bbo:
                return None
            bid = float(bbo.get("bid", 0) or 0)
            ask = float(bbo.get("ask", 0) or 0)
            if bid <= 0 or ask <= 0:
                return None
            mid = (bid + ask) / 2
            self._price_cache[market] = (mid, time.time())
            return mid
        except Exception as e:
            logger.error(f"[paradex] get_price {symbol}: {e}")
            return None

    def _market_meta(self, market: str) -> dict:
        if market in self._market_meta_cache:
            return self._market_meta_cache[market]
        try:
            resp = self.client.api_client.fetch_markets()
            for m in (resp.get("results") if isinstance(resp, dict) else []) or []:
                if m.get("symbol") == market:
                    meta = {
                        "tick_size": float(m.get("price_tick_size", 0.01) or 0.01),
                        "size_increment": float(m.get("order_size_increment", 0.001) or 0.001),
                        "min_notional": float(m.get("min_notional", 1.0) or 1.0),
                    }
                    self._market_meta_cache[market] = meta
                    return meta
        except Exception:
            pass
        return {"tick_size": 0.01, "size_increment": 0.001, "min_notional": 1.0}

    async def place_limit(self, symbol: str, side: str, price: float, size: float) -> Optional[str]:
        from paradex_py.common.order import Order, OrderType, OrderSide
        market = self._market_for(symbol)
        meta = self._market_meta(market)
        # Round price to tick, size to increment
        tick = meta["tick_size"]
        size_inc = meta["size_increment"]
        rounded_price = round(round(price / tick) * tick, 10)
        rounded_size = round(math.floor(size / size_inc) * size_inc, 10)
        if rounded_size <= 0:
            logger.warning(f"[paradex] size {size} rounds to 0 (inc={size_inc})")
            return None
        try:
            order = Order(
                market=market,
                order_type=OrderType.Limit,
                order_side=OrderSide.Buy if side.upper() == "BUY" else OrderSide.Sell,
                size=Decimal(str(rounded_size)),
                limit_price=Decimal(str(rounded_price)),
                instruction="POST_ONLY",
            )
            result = self.client.api_client.submit_order(order)
            if result and isinstance(result, dict):
                oid = result.get("id") or result.get("order_id")
                if oid:
                    return str(oid)
            logger.warning(f"[paradex] limit order returned no id: {result}")
            return None
        except Exception as e:
            logger.warning(f"[paradex] place_limit {symbol} failed: {e}")
            return None

    async def cancel_all(self, symbol: str) -> int:
        market = self._market_for(symbol)
        try:
            self.client.api_client.cancel_orders(market=market)
            return 1
        except Exception as e:
            logger.warning(f"[paradex] cancel_all {symbol}: {e}")
            return 0

    async def get_open_orders(self, symbol: str) -> List[dict]:
        market = self._market_for(symbol)
        try:
            resp = self.client.api_client.fetch_orders()
            return [o for o in (resp.get("results") if isinstance(resp, dict) else []) or []
                    if o.get("market") == market and o.get("status") == "OPEN"]
        except Exception:
            return []

    async def close_position(self, symbol: str) -> bool:
        """Maker-only close with widening + safety valve (mirrors Nado pattern)."""
        pos = await self.get_position(symbol)
        if not pos:
            self._stuck_first_attempt_ts.pop(symbol, None)
            return True

        now = time.time()
        if symbol not in self._stuck_first_attempt_ts:
            self._stuck_first_attempt_ts[symbol] = now
        stuck_minutes = (now - self._stuck_first_attempt_ts[symbol]) / 60.0

        is_buy_to_close = pos["side"] == "SHORT"
        side = "BUY" if is_buy_to_close else "SELL"
        for offset_bps in (1, 5, 15, 30):
            price = await self.get_price(symbol)
            if not price:
                break
            mult = (1 - offset_bps / 10_000) if is_buy_to_close else (1 + offset_bps / 10_000)
            limit_price = price * mult
            logger.info(
                f"[paradex] Maker close try ({offset_bps}bps): {side} {symbol} "
                f"@ ${limit_price:.4f} (mid=${price:.4f})"
            )
            oid = await self.place_limit(symbol, side, limit_price, pos["size"])
            if not oid:
                continue
            for _ in range(6):
                await asyncio.sleep(1)
                check = await self.get_position(symbol)
                if not check:
                    logger.info(f"[paradex] Maker close FILLED for {symbol} @ {offset_bps}bps")
                    self._stuck_first_attempt_ts.pop(symbol, None)
                    return True
                pos = check
            await self.cancel_all(symbol)
            await asyncio.sleep(0.5)

        # Safety valve — see NadoAdapter
        if stuck_minutes > self.STUCK_GRACE_MIN:
            logger.warning(
                f"[paradex] STUCK SAFETY VALVE: maker-only failed for "
                f"{stuck_minutes:.1f}min on {symbol}. Allowing one market close."
            )
            try:
                from paradex_py.common.order import Order, OrderType, OrderSide
                pos_now = await self.get_position(symbol)
                if pos_now and pos_now.get("size", 0) > 0:
                    market = self._market_for(symbol)
                    meta = self._market_meta(market)
                    rounded_size = round(
                        math.floor(pos_now["size"] / meta["size_increment"]) * meta["size_increment"],
                        10,
                    )
                    order = Order(
                        market=market,
                        order_type=OrderType.Market,
                        order_side=OrderSide.Buy if is_buy_to_close else OrderSide.Sell,
                        size=Decimal(str(rounded_size)),
                    )
                    result = self.client.api_client.submit_order(order)
                    if result:
                        self._stuck_first_attempt_ts.pop(symbol, None)
                        return True
            except Exception as e:
                logger.warning(f"[paradex] Safety-valve market close failed: {e}")

        logger.info(f"[paradex] Maker close gave up — will retry next cycle (stuck {stuck_minutes:.1f}min)")
        return False

    async def discover_markets(self) -> List[dict]:
        try:
            resp = self.client.api_client.fetch_markets()
            results = []
            for m in (resp.get("results") if isinstance(resp, dict) else []) or []:
                sym = m.get("symbol", "")
                if not sym.endswith("-USD-PERP"):
                    continue
                asset = sym.replace("-USD-PERP", "")
                results.append({
                    "asset": asset,
                    "symbol": sym,
                    "min_notional": float(m.get("min_notional", 1.0) or 1.0),
                })
            return results
        except Exception as e:
            logger.error(f"[paradex] discover_markets: {e}")
            return []


# ─── Factory ──────────────────────────────────────────────────────

def create_adapter(exchange: str) -> ExchangeAdapter:
    """Factory function to create the right adapter."""
    exchange = exchange.lower()
    if exchange == "hibachi":
        return HibachiAdapter()
    elif exchange == "nado":
        return NadoAdapter()
    elif exchange == "extended":
        return ExtendedAdapter()
    elif exchange == "paradex":
        return ParadexAdapter()
    else:
        raise ValueError(f"Unknown exchange: {exchange}. Use: hibachi, nado, extended, paradex")
