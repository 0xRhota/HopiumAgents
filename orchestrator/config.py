"""
Swing Orchestrator Configuration

All configurable parameters in one place.
"""

# =============================================================================
# PAPER TRADING MODE
# =============================================================================

PAPER_TRADE = True  # Set to True for paper trading with simulated balances
PAPER_BALANCE_PER_EXCHANGE = 100.0  # $100 per exchange in paper mode

# =============================================================================
# TIMING
# =============================================================================

CYCLE_INTERVAL_SECONDS = 900  # 15 minutes between decision cycles

# =============================================================================
# FUNDING RATE THRESHOLDS
# =============================================================================

# Funding rates are per 8-hour period (0.01% = 0.0001)
FUNDING_EXTREME_POSITIVE = 0.0003   # >0.03% - STRONG LONG signal (short squeeze)
FUNDING_MODERATE_POSITIVE = 0.0001  # >0.01% - WEAK LONG signal
FUNDING_MODERATE_NEGATIVE = -0.0001 # <-0.01% - WEAK SHORT signal
FUNDING_EXTREME_NEGATIVE = -0.0003  # <-0.03% - STRONG SHORT signal (long liquidations)

# =============================================================================
# TECHNICAL SCORE THRESHOLDS
# =============================================================================

SCORE_NO_TRADE = 2.5        # Below this = no trade
SCORE_TIER1_ONLY = 3.0      # 2.5-3.0 = only BTC, ETH
SCORE_STANDARD = 3.5        # 3.0-4.0 = standard swing
SCORE_HIGH_CONVICTION = 4.0 # >4.0 = high conviction, scalp allowed

# =============================================================================
# SHORT BIAS
# =============================================================================

# Historical data: SHORT WR 49.4% vs LONG WR 41.8% (7.6% edge)
SHORT_SCORE_BOOST = 0.5    # Add to SHORT signals
LONG_SCORE_PENALTY = 0.5   # Subtract from LONG signals (unless extreme funding)

# =============================================================================
# POSITION SIZING - LLM DRIVEN
# =============================================================================
# NOTE: Position sizing is now DYNAMIC and decided by the LLM based on:
# - Market conditions (funding, technicals, volatility)
# - Account balance and existing positions
# - LLM's own conviction level
#
# The LLM chooses whatever size it wants within these constraints:
# - Must be above exchange minimum (see EXCHANGE_CONFIG below)
# - Must not exceed 30% of available balance (soft limit in LLM prompt)
# - Must not exceed available balance
#
# We DO NOT force specific percentages. The LLM decides.
# As the user said: "If it is confident in some asset, let it buy more,
# but within the constraints of the account."

# Legacy constants kept for reference (NO LONGER USED):
# SIZE_HIGH_CONVICTION = 0.20  # Was 20%
# SIZE_STANDARD = 0.10         # Was 10%
# SIZE_SCALP = 0.15            # Was 15%

MAX_POSITIONS_PER_EXCHANGE = 2
MAX_TOTAL_POSITIONS = 4

# =============================================================================
# EXIT RULES
# =============================================================================

# High Conviction Swing
TP_HIGH_CONVICTION = 0.05      # 5% take profit
SL_HIGH_CONVICTION = 0.02      # 2% stop loss
TIME_STOP_HIGH_CONVICTION = 48 # 48 hours max hold

# Standard Swing
TP_STANDARD = 0.03             # 3% take profit
SL_STANDARD = 0.015            # 1.5% stop loss
TIME_STOP_STANDARD = 24        # 24 hours max hold

# Scalp
TP_SCALP = 0.015               # 1.5% take profit
SL_SCALP = 0.01                # 1% stop loss
TIME_STOP_SCALP = 2            # 2 hours max hold

# =============================================================================
# TRAILING STOP
# =============================================================================

TRAILING_BREAKEVEN_TRIGGER = 0.04  # Move to breakeven at +4%
TRAILING_START_TRIGGER = 0.06      # Start trailing at +6%
TRAILING_DISTANCE = 0.02           # Trail 2% below peak

# =============================================================================
# EXCHANGE-SPECIFIC
# =============================================================================

EXCHANGE_CONFIG = {
    "paradex": {
        "maker_fee": 0.0,
        "taker_fee": 0.0002,     # 0.02%
        "min_order_usd": 10,
        "assets": ["BTC-USD-PERP", "ETH-USD-PERP"],
        "enabled": True,
    },
    "hibachi": {
        "maker_fee": 0.0,
        "taker_fee": 0.00035,    # 0.035%
        "min_order_usd": 10,
        "assets": ["ETH/USDT-P", "SOL/USDT-P", "SUI/USDT-P", "XRP/USDT-P", "BTC/USDT-P"],
        "enabled": True,
    },
    "nado": {
        "maker_fee": 0.0,
        "taker_fee": 0.00035,    # 0.035% (but we use POST_ONLY)
        "min_order_usd": 100,    # CRITICAL: $100 minimum
        "assets": ["ETH-PERP", "BTC-PERP", "SOL-PERP"],
        "enabled": True,
    },
    "extended": {
        "maker_fee": 0.0,
        "taker_fee": 0.00025,    # 0.025%
        "min_order_usd": 10,
        "assets": ["BTC-USD", "ETH-USD", "SOL-USD", "XCU-USD"],
        "enabled": True,
    },
}

# =============================================================================
# TECHNICAL INDICATORS
# =============================================================================

RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

EMA_SHORT = 20
EMA_LONG = 50

VOLUME_SPIKE_MULTIPLIER = 2.0  # 2x average = spike

# =============================================================================
# LOGGING
# =============================================================================

LOG_DIR = "logs"
LOG_FILE = "swing_orchestrator.log"
DECISIONS_FILE = "swing_decisions.jsonl"
POSITIONS_FILE = "swing_positions.jsonl"
FUNDING_FILE = "swing_funding.jsonl"
PNL_FILE = "swing_pnl.jsonl"

# =============================================================================
# DATA VALIDATION
# =============================================================================

MAX_DATA_AGE_SECONDS = 120     # Data older than 2 min is stale
BALANCE_DRIFT_THRESHOLD = 0.01 # 1% drift = warning

# =============================================================================
# TIER 1 ASSETS (highest liquidity, safest)
# =============================================================================

TIER1_ASSETS = ["BTC", "ETH"]

# =============================================================================
# BLOCKED ASSETS (from historical data)
# =============================================================================

BLOCKED_ASSETS = ["DOGE"]  # 9% win rate historically
