#!/usr/bin/env python3.11
"""Quick script to open LONG positions on Paradex for bullish setup"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from paradex_py import ParadexSubkey
from paradex_py.common.order import Order, OrderType, OrderSide

def main():
    import httpx

    paradex = ParadexSubkey(
        env='prod',
        l2_private_key=os.getenv('PARADEX_PRIVATE_SUBKEY'),
        l2_address=os.getenv('PARADEX_ACCOUNT_ADDRESS'),
    )

    # Target: $50 LONG on BTC
    symbol = "BTC-USD-PERP"
    target_notional = 50.0

    # Get price via HTTP
    try:
        resp = httpx.get(f"https://api.prod.paradex.trade/v1/bbo/{symbol}")
        bbo = resp.json()
        bid = float(bbo.get('bid', 0))
        ask = float(bbo.get('ask', 0))
        mid_price = (bid + ask) / 2
        print(f"BTC mid price: ${mid_price:,.2f}")

        # Calculate size - must be multiple of 0.00001 (5 decimal places)
        size = target_notional / mid_price
        size = round(size, 5)

        print(f"Opening LONG: {symbol} size={size} (${target_notional})")

        # Place order using paradex client directly
        from decimal import Decimal
        order = Order(
            market=symbol,
            order_type=OrderType.Market,
            order_side=OrderSide.Buy,
            size=Decimal(str(size))
        )
        result = paradex.api_client.submit_order(order)
        print(f"✅ Result: {result}")

    except Exception as e:
        print(f"❌ Order failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
