#!/usr/bin/env python3.11
"""
Extended Bullish Setup - Open LONG positions for tariff play
PREVIOUS STATE (for reverting): Strategy D (Pairs Trade - Delta Neutral)
  - Command: python3.11 -m extended_agent.bot_extended --live --strategy D --interval 300

To revert to delta neutral:
  1. Close longs opened by this script
  2. Restart with: python3.11 -m extended_agent.bot_extended --live --strategy D --interval 300
"""

import os
import sys
import asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from dexes.extended.extended_sdk import ExtendedSDK


async def main():
    print("=" * 60)
    print("EXTENDED BULLISH SETUP")
    print("Opening LONG positions for tariff play")
    print("=" * 60)

    # Initialize SDK with credentials from env
    sdk = ExtendedSDK(
        api_key=os.getenv('EXTENDED_API_KEY'),
        stark_private_key=os.getenv('EXTENDED_STARK_PRIVATE_KEY'),
        stark_public_key=os.getenv('EXTENDED_STARK_PUBLIC_KEY'),
        vault=int(os.getenv('EXTENDED_VAULT', '0'))
    )

    # Get account balance
    balance_data = await sdk.get_balance()
    if balance_data:
        equity = float(balance_data.get('equity', 0))
        available = float(balance_data.get('availableBalance', 0))
        print(f"\nAccount equity: ${equity:.2f}")
        print(f"Available: ${available:.2f}")
    else:
        print("\nCould not fetch balance")
        equity = 50.0  # Default fallback

    # Get current positions
    positions = await sdk.get_positions()
    positions = positions or []
    print(f"\nCurrent positions: {len(positions)}")
    for pos in positions:
        symbol = pos.get('symbol') or pos.get('market')
        side = pos.get('side', '')
        size = pos.get('size', 0)
        pnl = pos.get('unrealizedPnl', pos.get('pnl', 0))
        print(f"  {symbol}: {side} size={size} PnL=${float(pnl):.2f}")

    # Close any SHORT positions first
    for pos in positions:
        symbol = pos.get('symbol') or pos.get('market')
        side = pos.get('side', '').upper()
        size = float(pos.get('size', 0))

        if side == 'SHORT' and size > 0:
            print(f"\n  Closing SHORT {symbol} (size={size})")
            try:
                result = await sdk.close_position(symbol)
                print(f"    Closed: {result}")
            except Exception as e:
                print(f"    Failed: {e}")

    # Target: Go LONG on BTC and ETH
    # Use 40% of equity for longs
    target_symbols = ["BTC-USD", "ETH-USD"]
    long_capital = equity * 0.40
    per_symbol = long_capital / len(target_symbols)

    print(f"\nOpening LONGs with ${long_capital:.2f} total (${per_symbol:.2f} each)")

    for symbol in target_symbols:
        # Skip if we already have a long
        has_long = any(
            (p.get('symbol') or p.get('market')) == symbol and p.get('side', '').upper() == 'LONG'
            for p in positions
        )
        if has_long:
            print(f"  {symbol}: Already LONG, skipping")
            continue

        # Get current price
        price = await sdk.get_price(symbol)
        if not price:
            print(f"  {symbol}: Could not get price, skipping")
            continue

        # Calculate size in base currency
        size = per_symbol / price
        print(f"  Opening LONG {symbol}: ${per_symbol:.2f} = {size:.6f} @ ${price:,.2f}")

        try:
            result = await sdk.create_market_order(
                market=symbol,
                is_buy=True,
                size=size
            )
            if result:
                print(f"    Result: {result.get('id', result)}")
            else:
                print(f"    Order may have failed - check positions")
        except Exception as e:
            print(f"    Failed: {e}")

    # Show final positions
    print("\n" + "=" * 60)
    print("FINAL POSITIONS")
    print("=" * 60)
    final_positions = await sdk.get_positions()
    final_positions = final_positions or []
    for pos in final_positions:
        symbol = pos.get('symbol') or pos.get('market')
        side = pos.get('side', '')
        size = pos.get('size', 0)
        pnl = pos.get('unrealizedPnl', pos.get('pnl', 0))
        print(f"  {symbol}: {side} size={size} PnL=${float(pnl):.2f}")

    print("\n" + "=" * 60)
    print("To revert to delta neutral:")
    print("  1. Close these longs manually or let them run")
    print("  2. python3.11 -m extended_agent.bot_extended --live --strategy D --interval 300")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
