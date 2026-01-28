#!/usr/bin/env python3
"""
Hibachi Grid MM Monitor - Check status every 5 minutes
"""
import asyncio
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

async def check_status():
    from dexes.hibachi.hibachi_sdk import HibachiSDK

    sdk = HibachiSDK(
        api_key=os.getenv('HIBACHI_PUBLIC_KEY'),
        api_secret=os.getenv('HIBACHI_PRIVATE_KEY'),
        account_id=os.getenv('HIBACHI_ACCOUNT_ID')
    )

    balance = await sdk.get_balance()
    positions = await sdk.get_positions()
    orders = await sdk.get_orders()

    ts = datetime.now().strftime("%H:%M:%S")

    # Count positions with non-zero quantity
    open_pos = [p for p in positions if float(p.get('quantity', 0)) != 0] if positions else []
    order_count = len(orders) if orders else 0

    # Calculate position value
    pos_value = 0
    pos_info = ""
    for p in open_pos:
        qty = float(p.get('quantity', 0))
        symbol = p.get('symbol', '')
        direction = p.get('direction', '')
        pos_info += f" | {symbol} {direction} {qty:.6f}"

    status = "OK" if order_count > 0 else "NO ORDERS"

    print(f"[{ts}] Balance: ${balance:.2f} | Orders: {order_count} | Positions: {len(open_pos)}{pos_info} | {status}")

    return balance, order_count

async def main():
    print("=== HIBACHI GRID MM MONITOR ===")
    print("Checking every 5 minutes. Ctrl+C to stop.\n")

    initial_balance = None

    while True:
        try:
            balance, orders = await check_status()
            if initial_balance is None:
                initial_balance = balance
            else:
                pnl = balance - initial_balance
                print(f"         Session PnL: ${pnl:+.2f}")

            await asyncio.sleep(300)  # 5 minutes
        except KeyboardInterrupt:
            print("\nMonitor stopped.")
            break
        except Exception as e:
            print(f"Error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
