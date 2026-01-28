#!/usr/bin/env python3
"""
Real P&L Tracker - Uses ACTUAL EXCHANGE DATA only.

NEVER trust local tracking. Always query the exchange.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def get_hibachi_data():
    """Get Hibachi account data from exchange."""
    from dexes.hibachi.hibachi_sdk import HibachiSDK

    sdk = HibachiSDK(
        api_key=os.getenv('HIBACHI_PUBLIC_KEY'),
        api_secret=os.getenv('HIBACHI_PRIVATE_KEY'),
        account_id=os.getenv('HIBACHI_ACCOUNT_ID')
    )

    balance = await sdk.get_balance()
    positions = await sdk.get_positions()

    open_pos = []
    for p in positions:
        symbol = p.get('symbol', p.get('market', 'unknown'))
        size = float(p.get('size', p.get('quantity', 0)))
        if size != 0:
            side = 'LONG' if size > 0 else 'SHORT'
            open_pos.append(f"{symbol} {side} {abs(size):.6f}")

    return {
        'exchange': 'Hibachi',
        'balance': balance or 0,
        'positions': open_pos,
        'realized_pnl_24h': None,  # API doesn't provide
        'realized_pnl_7d': None,
    }


async def get_nado_data():
    """Get Nado account data from exchange."""
    from dexes.nado.nado_sdk import NadoSDK

    sdk = NadoSDK(
        wallet_address=os.getenv('NADO_WALLET_ADDRESS'),
        linked_signer_private_key=os.getenv('NADO_LINKED_SIGNER_PRIVATE_KEY'),
        subaccount_name=os.getenv('NADO_SUBACCOUNT_NAME', 'default')
    )

    balance = await sdk.get_balance()
    positions = await sdk.get_positions()

    open_pos = []
    for p in positions:
        symbol = p.get('symbol', 'unknown')
        size = float(p.get('amount_float', 0))
        if size != 0:
            side = 'LONG' if size > 0 else 'SHORT'
            open_pos.append(f"{symbol} {side} {abs(size):.6f}")

    # Get real P&L from exchange
    pnl_24h = await sdk.get_pnl(hours=24)
    pnl_7d = await sdk.get_pnl(hours=168)

    return {
        'exchange': 'Nado',
        'balance': balance or 0,
        'positions': open_pos,
        'realized_pnl_24h': pnl_24h.get('net_pnl', 0),
        'realized_pnl_7d': pnl_7d.get('net_pnl', 0),
        'trades_24h': pnl_24h.get('trade_count', 0),
        'trades_7d': pnl_7d.get('trade_count', 0),
    }


async def get_paradex_data():
    """Get Paradex account data from exchange."""
    # Paradex requires Python 3.11
    import subprocess
    import json

    script = '''
import os
import sys
import json
sys.path.insert(0, '.')
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta

from paradex_py import ParadexSubkey

client = ParadexSubkey(
    env='prod',
    l2_private_key=os.getenv('PARADEX_PRIVATE_SUBKEY'),
    l2_address=os.getenv('PARADEX_ACCOUNT_ADDRESS')
)

account = client.api_client.fetch_account_summary()
positions_data = client.api_client.fetch_positions()

open_pos = []
if isinstance(positions_data, dict) and 'results' in positions_data:
    for p in positions_data['results']:
        if p['status'] == 'OPEN' and float(p['size']) != 0:
            size = float(p['size'])
            side = 'LONG' if size > 0 else 'SHORT'
            open_pos.append(f"{p['market']} {side} {abs(size):.6f}")

# Get fills for P&L
fills = client.api_client.fetch_fills()
pnl_24h = 0
pnl_7d = 0
count_24h = 0
count_7d = 0

if isinstance(fills, dict) and 'results' in fills:
    now = datetime.now()
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    for f in fills['results']:
        ts = f.get('created_at', 0)
        if ts:
            fill_time = datetime.fromtimestamp(ts / 1000)
            pnl = float(f.get('realized_pnl', 0))

            if fill_time > cutoff_7d:
                pnl_7d += pnl
                count_7d += 1

            if fill_time > cutoff_24h:
                pnl_24h += pnl
                count_24h += 1

result = {
    'balance': float(account.account_value),
    'positions': open_pos,
    'realized_pnl_24h': pnl_24h,
    'realized_pnl_7d': pnl_7d,
    'trades_24h': count_24h,
    'trades_7d': count_7d,
}
print(json.dumps(result))
'''

    python311 = '/opt/homebrew/Cellar/python@3.11/3.11.14_1/Frameworks/Python.framework/Versions/3.11/Resources/Python.app/Contents/MacOS/Python'

    try:
        result = subprocess.run(
            [python311, '-c', script],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        if result.returncode == 0:
            data = json.loads(result.stdout.strip())
            data['exchange'] = 'Paradex'
            return data
        else:
            print(f"Paradex error: {result.stderr}", file=sys.stderr)
    except Exception as e:
        print(f"Paradex error: {e}", file=sys.stderr)

    return {
        'exchange': 'Paradex',
        'balance': 0,
        'positions': [],
        'realized_pnl_24h': None,
        'realized_pnl_7d': None,
    }


async def main():
    print("=" * 60)
    print(f"P&L TRACKER - REAL EXCHANGE DATA")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # Fetch all data in parallel
    hibachi, nado, paradex = await asyncio.gather(
        get_hibachi_data(),
        get_nado_data(),
        get_paradex_data(),
        return_exceptions=True
    )

    exchanges = []
    for data in [hibachi, nado, paradex]:
        if isinstance(data, Exception):
            print(f"Error: {data}")
        else:
            exchanges.append(data)

    total_balance = 0
    total_pnl_24h = 0
    total_pnl_7d = 0
    has_24h = False
    has_7d = False

    for ex in exchanges:
        print(f"\n{ex['exchange'].upper()}")
        print("-" * 40)
        print(f"  Balance:     ${ex['balance']:.2f}")
        total_balance += ex['balance']

        if ex['positions']:
            print(f"  Positions:   {', '.join(ex['positions'])}")
        else:
            print(f"  Positions:   None")

        if ex.get('realized_pnl_24h') is not None:
            sign = '+' if ex['realized_pnl_24h'] >= 0 else ''
            print(f"  24h P&L:     {sign}${ex['realized_pnl_24h']:.2f} ({ex.get('trades_24h', '?')} trades)")
            total_pnl_24h += ex['realized_pnl_24h']
            has_24h = True

        if ex.get('realized_pnl_7d') is not None:
            sign = '+' if ex['realized_pnl_7d'] >= 0 else ''
            print(f"  7d P&L:      {sign}${ex['realized_pnl_7d']:.2f} ({ex.get('trades_7d', '?')} trades)")
            total_pnl_7d += ex['realized_pnl_7d']
            has_7d = True

    print("\n" + "=" * 60)
    print("TOTALS")
    print("=" * 60)
    print(f"  Total Balance: ${total_balance:.2f}")

    if has_24h:
        sign = '+' if total_pnl_24h >= 0 else ''
        emoji = '🟢' if total_pnl_24h >= 0 else '🔴'
        print(f"  24h Realized:  {sign}${total_pnl_24h:.2f} {emoji}")

    if has_7d:
        sign = '+' if total_pnl_7d >= 0 else ''
        emoji = '🟢' if total_pnl_7d >= 0 else '🔴'
        print(f"  7d Realized:   {sign}${total_pnl_7d:.2f} {emoji}")

    print()


if __name__ == '__main__':
    asyncio.run(main())
