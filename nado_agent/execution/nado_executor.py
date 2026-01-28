"""
Nado Trade Executor
Handles order placement and position management on Nado

Nado DEX features:
- Up to 20x leverage on perpetuals
- USDT0 settlement
- Low fees (maker rebates!)
- Built on Ink L2
"""

import logging
import time
import asyncio
from decimal import Decimal
from typing import Dict, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)


class NadoTradeExecutor:
    """
    Execute trades on Nado DEX
    """

    def __init__(
        self,
        nado_sdk,
        trade_tracker,
        data_fetcher,
        dry_run: bool = True,
        default_position_size: float = 10.0,
        max_positions: int = 10,
        max_spread_pct: float = 0.1
    ):
        """
        Initialize Nado executor

        Args:
            nado_sdk: NadoSDK instance
            trade_tracker: TradeTracker instance
            data_fetcher: NadoDataFetcher instance
            dry_run: If True, simulate trades
            default_position_size: USD per trade
            max_positions: Maximum open positions
            max_spread_pct: Max spread to accept for market orders
        """
        self.sdk = nado_sdk
        self.tracker = trade_tracker
        self.fetcher = data_fetcher
        self.dry_run = dry_run
        self.position_size = default_position_size
        self.max_positions = max_positions
        self.max_spread_pct = max_spread_pct

    def _get_full_symbol(self, symbol: str) -> str:
        """Convert base symbol to full Nado symbol"""
        if symbol.endswith('-PERP'):
            return symbol
        return f"{symbol}-PERP"

    async def _get_position_size(self, symbol: str) -> float:
        """
        Get current position size for a symbol

        Args:
            symbol: Base symbol (e.g., "BTC")

        Returns:
            Position size (positive for long, negative for short, 0 if none)
        """
        base = symbol.replace('-PERP', '')
        positions = await self.fetcher.fetch_positions()
        for pos in positions:
            if pos.get('symbol') == base:
                size = float(pos.get('size', 0))
                side = pos.get('side', 'LONG')
                return size if side == 'LONG' else -size
        return 0.0

    async def _verify_order_fill(
        self,
        symbol: str,
        expected_change: float,
        position_before: float,
        max_wait_seconds: float = 3.0
    ) -> Dict:
        """
        Verify that an order actually filled by checking position change

        Args:
            symbol: Base symbol
            expected_change: Expected position change (positive for buy, negative for sell)
            position_before: Position size before order was placed
            max_wait_seconds: Maximum time to wait for fill confirmation

        Returns:
            Dict with 'filled' boolean and details
        """
        await asyncio.sleep(0.5)

        checks = int(max_wait_seconds / 0.5)
        for i in range(checks):
            position_after = await self._get_position_size(symbol)
            actual_change = position_after - position_before

            if expected_change > 0:
                if actual_change > expected_change * 0.8:
                    logger.info(f"Order VERIFIED: position changed {position_before:.6f} -> {position_after:.6f}")
                    return {'filled': True, 'position_after': position_after, 'actual_change': actual_change}
            else:
                if actual_change < expected_change * 0.8:
                    logger.info(f"Order VERIFIED: position changed {position_before:.6f} -> {position_after:.6f}")
                    return {'filled': True, 'position_after': position_after, 'actual_change': actual_change}

            if i < checks - 1:
                await asyncio.sleep(0.5)

        logger.error(f"Order NOT FILLED: position unchanged at {position_before:.6f}")
        return {
            'filled': False,
            'error': 'Order accepted but not filled - likely rejected due to insufficient margin',
            'position_before': position_before,
            'position_after': await self._get_position_size(symbol)
        }

    async def _calculate_order_size(self, symbol: str, usd_amount: float) -> Optional[Decimal]:
        """
        Calculate order size based on USD amount and current price

        Args:
            symbol: Base symbol (e.g., "ETH")
            usd_amount: USD amount to trade

        Returns:
            Order size in base currency or None if error
        """
        bbo = await self.fetcher.fetch_bbo(symbol)
        if not bbo or bbo.get('mid_price', 0) <= 0:
            logger.error(f"Cannot get price for {symbol}")
            return None

        price = bbo['mid_price']
        size = usd_amount / price

        base = symbol.replace('-PERP', '')
        market_info = self.fetcher.market_info.get(base, {})
        step_size = market_info.get('step_size', 0.001)
        min_size = market_info.get('min_size', 0.001)

        if step_size >= 1:
            size = max(1, int(size))
        elif step_size > 0:
            size = float(int(size / step_size) * step_size)

        if size < min_size:
            logger.warning(f"Size {size} below minimum {min_size} for {symbol}")
            return None

        return Decimal(str(size))

    async def _check_spread(self, symbol: str) -> tuple:
        """
        Check if spread is acceptable for market order

        Returns:
            (is_acceptable, spread_pct)
        """
        bbo = await self.fetcher.fetch_bbo(symbol)
        if not bbo:
            return False, 999

        spread_pct = bbo.get('spread_pct', 999)
        is_acceptable = spread_pct <= self.max_spread_pct
        return is_acceptable, spread_pct

    async def execute_decision(self, decision: Dict) -> Dict:
        """
        Execute a trading decision

        Args:
            decision: Dict with keys:
                - action: "BUY", "SELL", "CLOSE"
                - symbol: Base symbol
                - confidence: 0.0-1.0
                - reason: String

        Returns:
            Dict with execution result
        """
        action = decision.get('action', '').upper()
        symbol = decision.get('symbol')
        confidence = decision.get('confidence', 0.5)
        reason = decision.get('reason', '')

        if not symbol:
            return {'success': False, 'error': 'No symbol provided'}

        if action == 'NOTHING':
            return {'success': True, 'action': 'NOTHING', 'message': 'No action taken'}

        is_ok, spread_pct = await self._check_spread(symbol)
        if not is_ok and action in ['BUY', 'SELL']:
            logger.warning(f"Spread too wide for {symbol}: {spread_pct:.3f}% > {self.max_spread_pct}%")
            return {
                'success': False,
                'error': f'Spread too wide: {spread_pct:.3f}%',
                'action': action,
                'symbol': symbol
            }

        if action == 'BUY':
            return await self._open_position(symbol, 'LONG', confidence, reason)
        elif action == 'SELL':
            return await self._open_position(symbol, 'SHORT', confidence, reason)
        elif action == 'CLOSE':
            return await self._close_position(symbol, reason)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    async def _open_position(
        self,
        symbol: str,
        side: str,
        confidence: float,
        reason: str
    ) -> Dict:
        """
        Open a new position

        Args:
            symbol: Base symbol
            side: "LONG" or "SHORT"
            confidence: Confidence score
            reason: Trade reason

        Returns:
            Execution result
        """
        base = symbol.replace('-PERP', '')

        positions = await self.fetcher.fetch_positions()
        for pos in positions:
            if pos.get('symbol') == base:
                return {
                    'success': False,
                    'error': f'Already have position in {base}',
                    'action': 'BUY' if side == 'LONG' else 'SELL',
                    'symbol': base
                }

        if len(positions) >= self.max_positions:
            return {
                'success': False,
                'error': f'Max positions ({self.max_positions}) reached',
                'action': 'BUY' if side == 'LONG' else 'SELL',
                'symbol': base
            }

        account = await self.fetcher.fetch_account_summary()
        account_balance = account.get('account_value', 0) if account else 0

        if account_balance and account_balance > 10:
            if confidence < 0.7:
                leverage = 4.0
                base_pct = 0.50
            elif confidence < 0.8:
                leverage = 5.0
                base_pct = 0.60
            elif confidence < 0.9:
                leverage = 4.0
                base_pct = 0.50
            else:
                leverage = 3.0
                base_pct = 0.40

            usd_amount = account_balance * base_pct * leverage
            usd_amount = max(50.0, min(usd_amount, 300.0))
            logger.info(f"Dynamic sizing: ${account_balance:.0f} x {base_pct:.0%} x {leverage}x = ${usd_amount:.2f}")
        else:
            usd_amount = max(self.position_size, 15.0)

        size = await self._calculate_order_size(base, usd_amount)

        if not size:
            return {
                'success': False,
                'error': 'Cannot calculate order size',
                'action': 'BUY' if side == 'LONG' else 'SELL',
                'symbol': base
            }

        bbo = await self.fetcher.fetch_bbo(base)
        entry_price = bbo.get('mid_price', 0) if bbo else 0

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would {side} {size} {base} @ ~${entry_price:.4f} (${usd_amount:.2f})")
            self.tracker.log_entry(
                order_id=f"dry_{base}_{int(time.time())}",
                symbol=base,
                side=side.lower(),
                entry_price=entry_price,
                size=float(size),
                confidence=confidence,
                notes=reason
            )
            return {
                'success': True,
                'action': 'BUY' if side == 'LONG' else 'SELL',
                'symbol': base,
                'size': float(size),
                'price': entry_price,
                'dry_run': True
            }

        max_retries = 3
        current_size = size
        current_usd = usd_amount
        position_before = await self._get_position_size(base)

        for attempt in range(max_retries + 1):
            try:
                is_buy = (side == 'LONG')
                result = await self.sdk.create_market_order(
                    symbol=self._get_full_symbol(base),
                    is_buy=is_buy,
                    amount=float(current_size)
                )

                if result and result.get('status') == 'success':
                    logger.info(f"Order accepted by API - verifying fill...")

                    expected_change = float(current_size) if is_buy else -float(current_size)
                    verify_result = await self._verify_order_fill(
                        symbol=base,
                        expected_change=expected_change,
                        position_before=position_before,
                        max_wait_seconds=3.0
                    )

                    if verify_result.get('filled'):
                        logger.info(f"Order FILLED: {side} {current_size} {base} @ ~${entry_price:.4f}")

                        self.tracker.log_entry(
                            order_id=result.get('order_id', f"nado_{base}_{int(time.time())}"),
                            symbol=base,
                            side=side.lower(),
                            entry_price=entry_price,
                            size=float(current_size),
                            confidence=confidence,
                            notes=reason
                        )

                        return {
                            'success': True,
                            'action': 'BUY' if side == 'LONG' else 'SELL',
                            'symbol': base,
                            'size': float(current_size),
                            'price': entry_price,
                            'order_id': result.get('order_id')
                        }
                    else:
                        logger.error(f"Order NOT FILLED for {base}: {verify_result.get('error')}")

                        if attempt < max_retries:
                            current_usd *= 0.5
                            current_size = await self._calculate_order_size(base, current_usd)
                            if not current_size:
                                break
                            position_before = await self._get_position_size(base)
                            logger.warning(f"[FILL-FAILED] Reducing to ${current_usd:.2f} (attempt {attempt + 2}/{max_retries + 1})")
                            continue

                        return {
                            'success': False,
                            'error': 'Order accepted but not filled - margin likely insufficient',
                            'action': 'BUY' if side == 'LONG' else 'SELL',
                            'symbol': base
                        }
                else:
                    error_msg = result.get('error', str(result)) if result else 'Unknown error'
                    return {
                        'success': False,
                        'error': f"Order rejected: {error_msg}",
                        'action': 'BUY' if side == 'LONG' else 'SELL',
                        'symbol': base
                    }

            except Exception as e:
                logger.error(f"Order error for {base}: {e}")
                if attempt < max_retries:
                    current_usd *= 0.5
                    current_size = await self._calculate_order_size(base, current_usd)
                    if not current_size:
                        break
                    logger.warning(f"[ERROR-RETRY] Reducing to ${current_usd:.2f} (attempt {attempt + 2}/{max_retries + 1})")
                    continue

                return {
                    'success': False,
                    'error': str(e),
                    'action': 'BUY' if side == 'LONG' else 'SELL',
                    'symbol': base
                }

        return {
            'success': False,
            'error': 'Failed after all retry attempts',
            'action': 'BUY' if side == 'LONG' else 'SELL',
            'symbol': base
        }

    async def _close_position(self, symbol: str, reason: str) -> Dict:
        """
        Close an existing position

        Args:
            symbol: Base symbol
            reason: Close reason

        Returns:
            Execution result
        """
        base = symbol.replace('-PERP', '')

        positions = await self.fetcher.fetch_positions()
        position = None
        for pos in positions:
            if pos.get('symbol') == base:
                position = pos
                break

        if not position:
            return {
                'success': False,
                'error': f'No position found for {base}',
                'action': 'CLOSE',
                'symbol': base
            }

        size = Decimal(str(position['size']))
        side = position['side']
        entry_price = position['entry_price']

        bbo = await self.fetcher.fetch_bbo(base)
        exit_price = bbo.get('mid_price', 0) if bbo else 0

        if side == 'LONG':
            pnl = (exit_price - entry_price) * float(size)
        else:
            pnl = (entry_price - exit_price) * float(size)

        if self.dry_run:
            logger.info(f"[DRY-RUN] Would CLOSE {side} {size} {base} @ ~${exit_price:.4f} (P&L: ${pnl:.2f})")
            order_id = self.tracker.get_order_id_for_symbol(base)
            if order_id:
                self.tracker.log_exit(order_id, exit_price, reason, fees=0)
            return {
                'success': True,
                'action': 'CLOSE',
                'symbol': base,
                'size': float(size),
                'price': exit_price,
                'pnl': pnl,
                'dry_run': True
            }

        try:
            is_buy = (side == 'SHORT')
            result = await self.sdk.create_market_order(
                symbol=self._get_full_symbol(base),
                is_buy=is_buy,
                amount=float(size),
                reduce_only=True
            )

            if result and result.get('status') == 'success':
                logger.info(f"Closed {side} {size} {base} @ ~${exit_price:.4f} (P&L: ${pnl:.2f})")

                order_id = self.tracker.get_order_id_for_symbol(base)
                if order_id:
                    self.tracker.log_exit(order_id, exit_price, reason, fees=0)

                return {
                    'success': True,
                    'action': 'CLOSE',
                    'symbol': base,
                    'size': float(size),
                    'price': exit_price,
                    'pnl': pnl,
                    'order_id': result.get('order_id')
                }
            else:
                error_msg = result.get('error', str(result)) if result else 'Unknown error'
                return {
                    'success': False,
                    'error': f"Close order rejected: {error_msg}",
                    'action': 'CLOSE',
                    'symbol': base
                }

        except Exception as e:
            logger.error(f"Close order error for {base}: {e}")
            return {
                'success': False,
                'error': str(e),
                'action': 'CLOSE',
                'symbol': base
            }

    async def close_all_positions(self, reason: str = "Manual close all") -> List[Dict]:
        """
        Close all open positions

        Returns:
            List of close results
        """
        positions = await self.fetcher.fetch_positions()
        results = []

        for pos in positions:
            symbol = pos.get('symbol')
            if symbol:
                result = await self._close_position(symbol, reason)
                results.append(result)

        return results
