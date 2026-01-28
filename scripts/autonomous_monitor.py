#!/usr/bin/env python3
"""
Autonomous Monitor - Runs in background, logs status, alerts on issues
Checks every 5 minutes, logs to logs/autonomous_monitor.log
"""

import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load env
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                os.environ[key] = val.strip('"').strip("'")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler('logs/autonomous_monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Alert thresholds
MAX_LOSS_PCT = -5.0  # Alert if position loss exceeds 5%
MAX_POSITION_NOTIONAL = 800.0  # Alert if position exceeds $800
STALE_LOG_MINUTES = 15  # Alert if log hasn't updated in 15 min


class AutonomousMonitor:
    def __init__(self):
        self.check_interval = 300  # 5 minutes
        self.last_check = None
        self.alerts_sent = set()

    async def check_hibachi(self) -> Dict:
        """Check Hibachi positions and balance"""
        try:
            from dexes.hibachi.hibachi_sdk import HibachiSDK
            sdk = HibachiSDK(
                os.getenv('HIBACHI_PUBLIC_KEY'),
                os.getenv('HIBACHI_PRIVATE_KEY'),
                os.getenv('HIBACHI_ACCOUNT_ID')
            )
            balance = await sdk.get_balance()
            positions = await sdk.get_positions()

            result = {
                'exchange': 'Hibachi',
                'balance': balance,
                'positions': [],
                'alerts': []
            }

            for p in positions:
                pos_info = {
                    'symbol': p.get('symbol', 'Unknown'),
                    'direction': p.get('direction', 'Unknown'),
                    'quantity': float(p.get('quantity', 0)),
                    'entry_price': float(p.get('openPrice', 0)),
                    'mark_price': float(p.get('markPrice', 0)),
                    'notional': float(p.get('notionalValue', 0)),
                    'unrealized_pnl': float(p.get('unrealizedTradingPnl', 0))
                }

                # Calculate P&L %
                if pos_info['notional'] > 0:
                    pnl_pct = (pos_info['unrealized_pnl'] / pos_info['notional']) * 100
                    pos_info['pnl_pct'] = pnl_pct

                    # Check alerts
                    if pnl_pct < MAX_LOSS_PCT:
                        alert = f"ALERT: {pos_info['symbol']} loss {pnl_pct:.1f}% exceeds {MAX_LOSS_PCT}%"
                        result['alerts'].append(alert)

                    if pos_info['notional'] > MAX_POSITION_NOTIONAL:
                        alert = f"ALERT: {pos_info['symbol']} notional ${pos_info['notional']:.0f} exceeds ${MAX_POSITION_NOTIONAL}"
                        result['alerts'].append(alert)

                result['positions'].append(pos_info)

            return result
        except Exception as e:
            return {'exchange': 'Hibachi', 'error': str(e), 'alerts': [f"ERROR: Hibachi check failed: {e}"]}

    async def check_nado(self) -> Dict:
        """Check Nado positions and balance"""
        try:
            from dexes.nado.nado_sdk import NadoSDK
            sdk = NadoSDK(
                wallet_address=os.getenv('NADO_WALLET_ADDRESS'),
                linked_signer_private_key=os.getenv('NADO_LINKED_SIGNER_PRIVATE_KEY'),
                subaccount_name=os.getenv('NADO_SUBACCOUNT_NAME')
            )
            balance = await sdk.get_balance()
            positions = await sdk.get_positions()

            result = {
                'exchange': 'Nado',
                'balance': balance,
                'positions': [],
                'alerts': []
            }

            for p in positions:
                pos_info = {
                    'symbol': p.get('symbol', 'Unknown'),
                    'amount': p.get('amount_float', 0),
                }
                result['positions'].append(pos_info)

            return result
        except Exception as e:
            return {'exchange': 'Nado', 'error': str(e), 'alerts': [f"ERROR: Nado check failed: {e}"]}

    def check_log_freshness(self, log_file: str, name: str) -> Optional[str]:
        """Check if log file has been updated recently"""
        try:
            if not os.path.exists(log_file):
                return f"ALERT: {name} log file not found"

            mtime = os.path.getmtime(log_file)
            age_minutes = (datetime.now().timestamp() - mtime) / 60

            if age_minutes > STALE_LOG_MINUTES:
                return f"ALERT: {name} log stale ({age_minutes:.0f} min old)"
            return None
        except Exception as e:
            return f"ERROR: Checking {name} log: {e}"

    async def run_check(self):
        """Run a full status check"""
        logger.info("=" * 60)
        logger.info(f"AUTONOMOUS MONITOR CHECK - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        all_alerts = []

        # Check Hibachi
        hibachi = await self.check_hibachi()
        logger.info(f"\nHibachi: Balance ${hibachi.get('balance', 0):.2f}")
        for pos in hibachi.get('positions', []):
            pnl_str = f"uPnL: ${pos['unrealized_pnl']:.2f} ({pos.get('pnl_pct', 0):.1f}%)" if 'unrealized_pnl' in pos else ""
            logger.info(f"  {pos['direction']} {pos['symbol']}: ${pos['notional']:.0f} {pnl_str}")
        all_alerts.extend(hibachi.get('alerts', []))

        # Check Nado
        nado = await self.check_nado()
        logger.info(f"\nNado: Balance ${nado.get('balance', 0):.2f}")
        for pos in nado.get('positions', []):
            logger.info(f"  {pos['symbol']}: {pos['amount']}")
        all_alerts.extend(nado.get('alerts', []))

        # Check log freshness
        logs_to_check = [
            ('logs/grid_mm_hibachi.log', 'Hibachi Grid MM'),
            ('logs/grid_mm_nado.log', 'Nado Grid MM'),
            ('logs/llm_supervisor.log', 'LLM Supervisor'),
        ]

        logger.info("\nLog Status:")
        for log_file, name in logs_to_check:
            alert = self.check_log_freshness(log_file, name)
            if alert:
                all_alerts.append(alert)
                logger.warning(f"  {name}: STALE")
            else:
                logger.info(f"  {name}: OK")

        # Log alerts
        if all_alerts:
            logger.warning("\n⚠️  ALERTS:")
            for alert in all_alerts:
                logger.warning(f"  {alert}")
        else:
            logger.info("\n✅ No alerts")

        logger.info(f"\nNext check in {self.check_interval // 60} minutes")
        self.last_check = datetime.now()

    async def run(self):
        """Main monitoring loop"""
        logger.info("Starting Autonomous Monitor...")
        logger.info(f"Check interval: {self.check_interval}s")
        logger.info(f"Loss alert threshold: {MAX_LOSS_PCT}%")
        logger.info(f"Position alert threshold: ${MAX_POSITION_NOTIONAL}")

        while True:
            try:
                await self.run_check()
            except Exception as e:
                logger.error(f"Monitor check failed: {e}")

            await asyncio.sleep(self.check_interval)


if __name__ == "__main__":
    monitor = AutonomousMonitor()
    asyncio.run(monitor.run())
