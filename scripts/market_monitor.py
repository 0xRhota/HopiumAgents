#!/usr/bin/env python3
"""
Market Monitor - Stops bots during choppy conditions, restarts when calm.

Monitors ETH price volatility and only runs bots when market is ranging (favorable).
"""

import asyncio
import os
import sys
import signal
import subprocess
from datetime import datetime, timedelta
from collections import deque
from dotenv import load_dotenv

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

# Configuration
CHECK_INTERVAL = 30  # seconds between checks
VOLATILITY_WINDOW = 300  # 5 minutes of price history
MAX_VOLATILITY_BPS = 15  # max acceptable volatility (std dev in bps)
MIN_CALM_DURATION = 600  # 10 minutes of calm before restarting
RECHECK_AFTER_START = 300  # 5 minutes before checking again after restart

LOG_FILE = "logs/market_monitor.log"

class MarketMonitor:
    def __init__(self):
        self.prices = deque(maxlen=int(VOLATILITY_WINDOW / CHECK_INTERVAL) + 1)
        self.timestamps = deque(maxlen=int(VOLATILITY_WINDOW / CHECK_INTERVAL) + 1)
        self.calm_since = None
        self.bots_running = False
        self.sdk = None

    def log(self, msg: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} | {msg}"
        print(line)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")

    async def init_sdk(self):
        """Initialize Nado SDK for price fetching"""
        try:
            from dexes.nado.nado_sdk import NadoSDK

            wallet_address = os.getenv("NADO_WALLET_ADDRESS")
            signer_key = os.getenv("NADO_LINKED_SIGNER_PRIVATE_KEY")
            subaccount = os.getenv("NADO_SUBACCOUNT_NAME", "default")

            self.sdk = NadoSDK(
                wallet_address=wallet_address,
                linked_signer_private_key=signer_key,
                subaccount_name=subaccount
            )
            # SDK is ready after construction - no separate init needed
            self.log("SDK initialized for price monitoring")
            return True
        except Exception as e:
            self.log(f"Failed to init SDK: {e}")
            return False

    async def get_eth_price(self) -> float:
        """Get current ETH price from Nado"""
        try:
            response = await self.sdk._query("market_price", {"product_id": "4"})
            if response.get("status") == "success":
                data = response.get("data", {})
                bid = self.sdk._from_x18(int(data.get('bid_x18', '0')))
                ask = self.sdk._from_x18(int(data.get('ask_x18', '0')))
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2
        except Exception as e:
            self.log(f"Price fetch error: {e}")
        return 0

    def calculate_volatility(self) -> float:
        """Calculate volatility in bps (standard deviation of returns)"""
        if len(self.prices) < 3:
            return 999  # Not enough data, assume high volatility

        prices = list(self.prices)
        returns_bps = []
        for i in range(1, len(prices)):
            if prices[i-1] > 0:
                ret = (prices[i] - prices[i-1]) / prices[i-1] * 10000  # bps
                returns_bps.append(ret)

        if len(returns_bps) < 2:
            return 999

        # Standard deviation of returns
        mean = sum(returns_bps) / len(returns_bps)
        variance = sum((r - mean) ** 2 for r in returns_bps) / len(returns_bps)
        std_dev = variance ** 0.5

        return std_dev

    def stop_bots(self):
        """Stop all grid MM bots"""
        self.log("STOPPING all bots...")
        try:
            subprocess.run(["pkill", "-f", "grid_mm_nado_v8.py"], capture_output=True)
            subprocess.run(["pkill", "-f", "grid_mm_hibachi_v8.py"], capture_output=True)
            self.bots_running = False
            self.log("Bots stopped")
        except Exception as e:
            self.log(f"Error stopping bots: {e}")

    def start_bots(self):
        """Start all grid MM bots"""
        self.log("STARTING bots - market conditions favorable...")
        try:
            # Start Nado bot
            subprocess.Popen(
                ["python3", "-u", "scripts/grid_mm_nado_v8.py"],
                stdout=open("logs/grid_mm_nado_v8.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
            self.log("  Started Nado v8")

            # Start Hibachi bot
            subprocess.Popen(
                ["python3", "-u", "scripts/grid_mm_hibachi_v8.py"],
                stdout=open("logs/grid_mm_hibachi_v8.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
            self.log("  Started Hibachi v8")

            self.bots_running = True
        except Exception as e:
            self.log(f"Error starting bots: {e}")

    def check_bots_running(self) -> bool:
        """Check if bots are currently running"""
        result = subprocess.run(
            ["pgrep", "-f", "grid_mm_(nado|hibachi)_v8.py"],
            capture_output=True
        )
        return result.returncode == 0

    async def run(self):
        """Main monitoring loop"""
        self.log("=" * 60)
        self.log("MARKET MONITOR STARTED")
        self.log(f"  Max volatility: {MAX_VOLATILITY_BPS} bps")
        self.log(f"  Calm duration required: {MIN_CALM_DURATION}s")
        self.log("=" * 60)

        # Initialize SDK
        if not await self.init_sdk():
            self.log("Cannot continue without SDK")
            return

        # Stop bots immediately
        self.stop_bots()

        while True:
            try:
                # Get current price
                price = await self.get_eth_price()
                if price > 0:
                    self.prices.append(price)
                    self.timestamps.append(datetime.now())

                # Calculate volatility
                volatility = self.calculate_volatility()

                # Determine market state
                is_calm = volatility < MAX_VOLATILITY_BPS

                if is_calm:
                    if self.calm_since is None:
                        self.calm_since = datetime.now()
                        self.log(f"Market calming... vol={volatility:.1f} bps, price=${price:.2f}")

                    calm_duration = (datetime.now() - self.calm_since).total_seconds()

                    if not self.bots_running and calm_duration >= MIN_CALM_DURATION:
                        self.log(f"Market calm for {calm_duration:.0f}s - RESTARTING BOTS")
                        self.start_bots()
                        # Wait before checking again
                        await asyncio.sleep(RECHECK_AFTER_START)
                        continue
                    elif not self.bots_running:
                        remaining = MIN_CALM_DURATION - calm_duration
                        self.log(f"Calm: vol={volatility:.1f} bps, need {remaining:.0f}s more")
                else:
                    # Choppy market
                    if self.calm_since is not None:
                        self.log(f"Market choppy again - vol={volatility:.1f} bps")
                    self.calm_since = None

                    if self.bots_running:
                        self.log(f"CHOPPY DETECTED: vol={volatility:.1f} bps - stopping bots")
                        self.stop_bots()
                    else:
                        self.log(f"Choppy: vol={volatility:.1f} bps, price=${price:.2f}, waiting...")

                await asyncio.sleep(CHECK_INTERVAL)

            except Exception as e:
                self.log(f"Monitor error: {e}")
                await asyncio.sleep(CHECK_INTERVAL)


def signal_handler(sig, frame):
    print("\nMonitor interrupted - bots remain stopped")
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    os.chdir("/Users/admin/Documents/Projects/pacifica-trading-bot")

    monitor = MarketMonitor()
    asyncio.run(monitor.run())
