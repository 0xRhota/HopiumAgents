#!/usr/bin/env python3
"""
Nado Grid Bot Monitor - Check P&L every 5 minutes
"""
import asyncio
import os
import time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

async def check_status():
    from dexes.nado.nado_sdk import NadoSDK

    sdk = NadoSDK(
        wallet_address=os.getenv('NADO_WALLET_ADDRESS'),
        linked_signer_private_key=os.getenv('NADO_LINKED_SIGNER_PRIVATE_KEY')
    )

    balance = await sdk.get_balance()
    pnl_1h = await sdk.get_pnl(hours=1)
    pnl_15m = await sdk.get_pnl(hours=0.25)

    ts = datetime.now().strftime("%H:%M:%S")

    trades_1h = pnl_1h.get("trade_count", 0)
    net_1h = pnl_1h.get("net_pnl", 0)
    trades_15m = pnl_15m.get("trade_count", 0)
    net_15m = pnl_15m.get("net_pnl", 0)

    per_trade_1h = net_1h / trades_1h if trades_1h > 0 else 0
    per_trade_15m = net_15m / trades_15m if trades_15m > 0 else 0

    status = "OK" if net_15m >= 0 else "LOSING"

    print(f"[{ts}] Balance: ${balance:.2f} | 15m: ${net_15m:+.2f} ({trades_15m}t, ${per_trade_15m:+.3f}/t) | 1h: ${net_1h:+.2f} ({trades_1h}t) | {status}")

    return net_15m, trades_15m

async def main():
    print("=== NADO v13 MONITOR ===")
    print("Checking every 5 minutes. Ctrl+C to stop.\n")

    while True:
        try:
            await check_status()
            await asyncio.sleep(300)  # 5 minutes
        except KeyboardInterrupt:
            print("\nMonitor stopped.")
            break
        except Exception as e:
            print(f"Error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
