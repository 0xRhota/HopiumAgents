#!/usr/bin/env python3
"""
Quick script to close Nado positions
"""

import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from dexes.nado.nado_sdk import NadoSDK


async def main():
    wallet_address = os.getenv("NADO_WALLET_ADDRESS")
    linked_signer_key = os.getenv("NADO_LINKED_SIGNER_PRIVATE_KEY")
    subaccount_name = os.getenv("NADO_SUBACCOUNT_NAME", "default")

    if not wallet_address or not linked_signer_key:
        print("Missing NADO credentials in .env")
        return

    sdk = NadoSDK(
        wallet_address=wallet_address,
        linked_signer_private_key=linked_signer_key,
        subaccount_name=subaccount_name,
        testnet=False
    )

    # Get current positions
    print("Fetching positions...")
    positions = await sdk.get_positions()

    if not positions:
        print("No open positions to close")
        return

    print(f"Found {len(positions)} position(s):")
    for pos in positions:
        symbol = pos.get("symbol")
        amount = pos.get("amount_float", 0)
        side = "LONG" if amount > 0 else "SHORT"
        print(f"  {symbol}: {abs(amount):.6f} {side}")

    # Close each position
    for pos in positions:
        symbol = pos.get("symbol")
        amount = pos.get("amount_float", 0)

        if amount == 0:
            continue

        # To close: sell if long, buy if short
        is_buy = amount < 0  # Close short by buying
        close_amount = abs(amount)

        print(f"\nClosing {symbol} position: {'BUY' if is_buy else 'SELL'} {close_amount}...")

        result = await sdk.create_market_order(
            symbol=symbol,
            is_buy=is_buy,
            amount=close_amount,
            reduce_only=True
        )

        if result and result.get("status") == "success":
            print(f"  ✅ Position closed successfully")
        else:
            print(f"  ❌ Failed to close: {result}")

    # Verify positions are closed
    print("\nVerifying positions...")
    positions = await sdk.get_positions()
    if not positions:
        print("✅ All positions closed")
    else:
        print(f"⚠️  Still have {len(positions)} position(s)")
        for pos in positions:
            print(f"  {pos}")


if __name__ == "__main__":
    asyncio.run(main())
