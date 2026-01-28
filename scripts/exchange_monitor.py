#!/usr/bin/env python3.11
"""
Exchange Monitor v1 - Real exchange data monitoring.

Queries each exchange's REAL API for fills, balances, and P&L.
No local tracking, no vanished-order guessing, no log parsing.

Usage:
    python3 scripts/exchange_monitor.py              # One-shot status check
    python3 scripts/exchange_monitor.py --loop 300   # Check every 5 minutes
"""
import asyncio
import os
import sys
import argparse
from datetime import datetime, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv('.env')

import logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


async def check_nado():
    """Query Nado Archive API for real fills and P&L."""
    try:
        from dexes.nado.nado_sdk import NadoSDK
        import time

        sdk = NadoSDK(
            wallet_address=os.getenv('NADO_WALLET_ADDRESS'),
            linked_signer_private_key=os.getenv('NADO_LINKED_SIGNER_PRIVATE_KEY'),
            subaccount_name=os.getenv('NADO_SUBACCOUNT_NAME', 'default'),
        )

        # Get current balance
        balance = await sdk.get_balance()

        # Query matches directly for full fill details including volume
        payload = {
            "matches": {
                "subaccounts": [sdk._get_subaccount_bytes32()],
                "limit": 500,
                "isolated": False
            }
        }
        response = await sdk._archive_query(payload)
        matches = response.get("matches", [])
        txs = response.get("txs", [])

        # Build timestamp lookup
        tx_timestamps = {}
        for tx in txs:
            idx = tx.get("submission_idx")
            ts = int(tx.get("timestamp", 0))
            tx_timestamps[idx] = ts

        now = int(time.time())
        cutoff_1h = now - 3600
        cutoff_24h = now - 86400

        def summarize_period(cutoff):
            trade_count = 0
            volume = 0.0
            realized_pnl = 0.0
            fees = 0.0
            maker = 0

            for match in matches:
                idx = match.get("submission_idx")
                ts = tx_timestamps.get(idx, 0)
                if ts >= cutoff:
                    trade_count += 1
                    quote_filled = abs(float(match.get("quote_filled", "0"))) / 1e18
                    volume += quote_filled
                    realized_pnl += float(match.get("realized_pnl", "0")) / 1e18
                    fees += abs(float(match.get("fee", "0"))) / 1e18
                    if not match.get("is_taker", True):
                        maker += 1

            return {
                'trade_count': trade_count,
                'volume': volume,
                'realized_pnl': realized_pnl,
                'fees': fees,
                'net_pnl': realized_pnl - fees,
                'maker_pct': (maker / trade_count * 100) if trade_count else 0,
            }

        result = {
            'exchange': 'Nado (ETH)',
            'balance': balance,
            '1h': summarize_period(cutoff_1h),
            '24h': summarize_period(cutoff_24h),
        }
        return result

    except Exception as e:
        return {'exchange': 'Nado (ETH)', 'error': str(e)}


async def check_paradex():
    """Query Paradex fills API for real fills and P&L."""
    try:
        from paradex_py import ParadexSubkey

        private_key = os.getenv("PARADEX_PRIVATE_SUBKEY")
        client = ParadexSubkey(
            env='prod',
            l2_private_key=private_key,
            l2_address=os.getenv('PARADEX_ACCOUNT_ADDRESS'),
        )

        # Get account balance
        account = client.api_client.fetch_account_summary()
        balance = float(account.account_value)

        # Get fills
        fills_data = client.api_client.fetch_fills()
        fills_1h = []
        fills_24h = []
        now = datetime.now()

        if fills_data and fills_data.get('results'):
            for f in fills_data['results']:
                ts = f.get('created_at', 0)
                if ts:
                    fill_time = datetime.fromtimestamp(ts / 1000)
                    age = now - fill_time
                    if age <= timedelta(hours=1):
                        fills_1h.append(f)
                    if age <= timedelta(hours=24):
                        fills_24h.append(f)
                    else:
                        break  # Fills are sorted newest first

        def summarize_fills(fills):
            volume = sum(float(f.get('price', 0)) * float(f.get('size', 0)) for f in fills)
            pnl = sum(float(f.get('realized_pnl', 0)) for f in fills)
            fees = sum(float(f.get('fee', 0)) for f in fills)
            maker = sum(1 for f in fills if f.get('liquidity') == 'MAKER')
            return {
                'trade_count': len(fills),
                'volume': volume,
                'realized_pnl': pnl,
                'fees': fees,
                'maker_pct': (maker / len(fills) * 100) if fills else 0,
            }

        # Get position
        positions = client.api_client.fetch_positions()
        pos_info = []
        if positions and positions.get('results'):
            for p in positions['results']:
                size = float(p.get('size', 0))
                if size != 0:
                    pos_info.append({
                        'market': p.get('market'),
                        'size': size,
                        'unrealized_pnl': float(p.get('unrealized_pnl', 0)),
                    })

        return {
            'exchange': 'Paradex (BTC)',
            'balance': balance,
            'positions': pos_info,
            '1h': summarize_fills(fills_1h),
            '24h': summarize_fills(fills_24h),
        }

    except Exception as e:
        return {'exchange': 'Paradex (BTC)', 'error': str(e)}


async def check_extended():
    """Query Extended x10 API for real trades and balance."""
    try:
        from x10.perpetual.configuration import MAINNET_CONFIG
        from x10.perpetual.trading_client import PerpetualTradingClient
        from x10.perpetual.accounts import StarkPerpetualAccount

        account = StarkPerpetualAccount(
            vault=int(os.getenv('EXTENDED_VAULT')),
            private_key=os.getenv('EXTENDED_STARK_PRIVATE_KEY'),
            public_key=os.getenv('EXTENDED_STARK_PUBLIC_KEY'),
            api_key=os.getenv('EXTENDED_API_KEY'),
        )
        client = PerpetualTradingClient(endpoint_config=MAINNET_CONFIG, stark_account=account)

        # Get balance
        balance_resp = await client.account.get_balance()
        balance = float(balance_resp.data.balance) if balance_resp.data else 0
        equity = float(balance_resp.data.equity) if balance_resp.data else 0
        unrealized = float(balance_resp.data.unrealised_pnl) if balance_resp.data else 0

        # Get trades
        trades_resp = await client.account.get_trades(market_names=['BTC-USD'], limit=200)
        trades_1h = []
        trades_24h = []
        now_ms = int(datetime.now().timestamp() * 1000)

        if trades_resp and trades_resp.data:
            for t in trades_resp.data:
                age_ms = now_ms - t.created_time
                if age_ms <= 3600 * 1000:
                    trades_1h.append(t)
                if age_ms <= 86400 * 1000:
                    trades_24h.append(t)
                else:
                    break

        def summarize_trades(trades):
            volume = sum(float(t.value) for t in trades)
            fees = sum(float(t.fee) for t in trades)
            maker = sum(1 for t in trades if not t.is_taker)
            return {
                'trade_count': len(trades),
                'volume': volume,
                'fees': fees,
                'maker_pct': (maker / len(trades) * 100) if trades else 0,
            }

        # Get positions
        pos_resp = await client.account.get_positions()
        pos_info = []
        if pos_resp and pos_resp.data:
            for p in pos_resp.data:
                size = float(p.qty) if hasattr(p, 'qty') else 0
                if size != 0:
                    pos_info.append({
                        'market': p.market if hasattr(p, 'market') else '?',
                        'size': size,
                    })

        return {
            'exchange': 'Extended (BTC)',
            'balance': balance,
            'equity': equity,
            'unrealized_pnl': unrealized,
            'positions': pos_info,
            '1h': summarize_trades(trades_1h),
            '24h': summarize_trades(trades_24h),
        }

    except Exception as e:
        return {'exchange': 'Extended (BTC)', 'error': str(e)}


async def check_hibachi():
    """Query Hibachi SDK for balance and positions."""
    try:
        from dexes.hibachi.hibachi_sdk import HibachiSDK

        api_key = os.getenv('HIBACHI_API_KEY')
        api_secret = os.getenv('HIBACHI_API_SECRET')
        account_id = os.getenv('HIBACHI_ACCOUNT_ID')

        if not all([api_key, api_secret, account_id]):
            return {'exchange': 'Hibachi (BTC)', 'error': 'Missing credentials'}

        sdk = HibachiSDK(api_key, api_secret, account_id)

        balance = await sdk.get_balance()
        positions = await sdk.get_positions()

        pos_info = []
        for p in (positions or []):
            size = float(p.get('size', 0))
            if size != 0:
                pos_info.append({
                    'market': p.get('market', '?'),
                    'size': size,
                })

        return {
            'exchange': 'Hibachi (BTC)',
            'balance': balance,
            'positions': pos_info,
            'note': 'No fill history API available',
        }

    except Exception as e:
        return {'exchange': 'Hibachi (BTC)', 'error': str(e)}


def print_report(results):
    """Print formatted report of all exchange data."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*70}")
    print(f"  EXCHANGE MONITOR - {now}")
    print(f"{'='*70}")

    total_balance = 0.0

    for r in results:
        print(f"\n  {r['exchange']}")
        print(f"  {'-'*40}")

        if 'error' in r:
            print(f"    ERROR: {r['error']}")
            continue

        bal = r.get('balance', 0) or 0
        total_balance += bal
        print(f"    Balance: ${bal:.2f}", end='')
        if 'equity' in r:
            print(f" | Equity: ${r['equity']:.2f}", end='')
        if 'unrealized_pnl' in r:
            print(f" | Unrealized: ${r['unrealized_pnl']:.4f}", end='')
        print()

        # Positions
        if r.get('positions'):
            for p in r['positions']:
                side = 'LONG' if p['size'] > 0 else 'SHORT'
                print(f"    Position: {p.get('market','?')} {side} {abs(p['size']):.6f}", end='')
                if 'unrealized_pnl' in p:
                    print(f" (uPnL: ${p['unrealized_pnl']:.4f})", end='')
                print()

        # 1h stats
        if '1h' in r and not isinstance(r['1h'], str):
            s = r['1h']
            tc = s.get('trade_count', 0)
            vol = s.get('volume', 0)
            pnl = s.get('realized_pnl', s.get('net_pnl', 0))
            fees = s.get('fees', 0)
            maker = s.get('maker_pct', 0)
            print(f"    1h:  {tc} trades | ${vol:,.2f} vol | pnl=${pnl:.4f} | fees=${fees:.4f}", end='')
            if maker:
                print(f" | {maker:.0f}% maker", end='')
            print()

        # 24h stats
        if '24h' in r and not isinstance(r['24h'], str):
            s = r['24h']
            tc = s.get('trade_count', 0)
            vol = s.get('volume', 0)
            pnl = s.get('realized_pnl', s.get('net_pnl', 0))
            fees = s.get('fees', 0)
            maker = s.get('maker_pct', 0)
            print(f"    24h: {tc} trades | ${vol:,.2f} vol | pnl=${pnl:.4f} | fees=${fees:.4f}", end='')
            if maker:
                print(f" | {maker:.0f}% maker", end='')
            print()

        if r.get('note'):
            print(f"    Note: {r['note']}")

    print(f"\n  {'='*40}")
    print(f"  TOTAL BALANCE: ${total_balance:.2f}")
    print(f"  {'='*40}\n")


async def check_processes():
    """Check which bot processes are running."""
    import subprocess
    bots = [
        ("Nado Grid MM", "grid_mm_nado_v8.py"),
        ("Paradex Grid MM", "grid_mm_live.py"),
        ("Extended Grid MM", "grid_mm_extended.py"),
        ("Hibachi Grid MM", "grid_mm_hibachi.py"),
        ("Hibachi LLM", "hibachi_agent.bot_hibachi"),
        ("Watchdog", "watchdog.py"),
    ]

    print("\n  BOT PROCESSES")
    print(f"  {'-'*40}")
    for name, pattern in bots:
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True, text=True
            )
            running = result.returncode == 0
            status = "RUNNING" if running else "DOWN"
            print(f"    {name}: {status}")
        except Exception:
            print(f"    {name}: UNKNOWN")


async def main():
    parser = argparse.ArgumentParser(description='Exchange Monitor - Real API data')
    parser.add_argument('--loop', type=int, help='Check interval in seconds (runs continuously)')
    parser.add_argument('--no-hibachi', action='store_true', help='Skip Hibachi check')
    args = parser.parse_args()

    while True:
        # Check processes first
        await check_processes()

        # Query all exchanges in parallel
        tasks = [check_nado(), check_paradex(), check_extended()]
        if not args.no_hibachi:
            tasks.append(check_hibachi())

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle exceptions
        clean_results = []
        for r in results:
            if isinstance(r, Exception):
                clean_results.append({'exchange': '?', 'error': str(r)})
            else:
                clean_results.append(r)

        print_report(clean_results)

        if not args.loop:
            break

        await asyncio.sleep(args.loop)


if __name__ == "__main__":
    asyncio.run(main())
