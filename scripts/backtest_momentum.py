#!/usr/bin/env python3
"""
Momentum Limit Order Strategy - Backtest Engine
Compares momentum limits vs grid MM across different market conditions.

Usage:
    python3 scripts/backtest_momentum.py --hours 72
    python3 scripts/backtest_momentum.py --hours 336   # 2 weeks
    python3 scripts/backtest_momentum.py --hours 72 --symbol ETHUSDT
"""

import argparse
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import requests


# ─── Configuration ───────────────────────────────────────────────

@dataclass
class MomentumConfig:
    """Momentum limit order strategy config."""
    offset_bps: float = 7.0       # How far behind price to place limit (bps)
    tp_bps: float = 15.0          # Take profit (bps)
    sl_bps: float = 10.0          # Stop loss (bps)
    trend_candles: int = 3        # Number of candles to determine trend
    trend_threshold: int = 2      # Min candles in same direction for trend
    order_size_usd: float = 100   # Notional per order
    max_positions: int = 1        # Max simultaneous positions
    refresh_candles: int = 1      # Re-evaluate every N candles


@dataclass
class GridConfig:
    """Grid MM strategy config."""
    spread_bps: float = 15.0      # Half-spread from mid price
    order_size_usd: float = 100   # Notional per order per level
    levels: int = 2               # Levels per side
    max_inventory_pct: float = 1.5  # Max leverage before pausing
    balance: float = 80.0         # Account balance


@dataclass
class Trade:
    """A completed trade."""
    strategy: str
    symbol: str
    side: str           # LONG or SHORT
    entry_price: float
    exit_price: float
    size_usd: float
    entry_time: str
    exit_time: str
    exit_reason: str    # TP, SL, TIME, TREND_FLIP
    pnl_usd: float = 0.0
    pnl_bps: float = 0.0
    hold_candles: int = 0


# ─── Data Fetching ───────────────────────────────────────────────

def fetch_klines(symbol: str, interval: str, hours: int) -> list:
    """Fetch historical klines from Binance."""
    all_klines = []
    end_time = int(time.time() * 1000)
    candles_needed = hours * (60 // 5)  # 5m candles

    print(f"Fetching {candles_needed} candles for {symbol} ({hours}h)...")

    while len(all_klines) < candles_needed:
        remaining = candles_needed - len(all_klines)
        limit = min(remaining, 1000)

        params = {
            'symbol': symbol,
            'interval': interval,
            'endTime': end_time,
            'limit': limit
        }

        resp = requests.get('https://api.binance.com/api/v3/klines', params=params, timeout=10)
        data = resp.json()

        if not data:
            break

        all_klines = data + all_klines
        end_time = data[0][0] - 1  # Go further back

        if len(data) < limit:
            break

        time.sleep(0.2)  # Rate limit

    print(f"  Got {len(all_klines)} candles")
    return all_klines[:candles_needed]


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    time_str: str = ""

    def __post_init__(self):
        self.time_str = datetime.fromtimestamp(self.timestamp / 1000).strftime("%m/%d %H:%M")


def parse_klines(raw: list) -> List[Candle]:
    return [
        Candle(
            timestamp=k[0],
            open=float(k[1]),
            high=float(k[2]),
            low=float(k[3]),
            close=float(k[4]),
            volume=float(k[5])
        )
        for k in raw
    ]


# ─── Momentum Strategy ──────────────────────────────────────────

def detect_trend(candles: List[Candle], n: int = 3, threshold: int = 2) -> Optional[str]:
    """Detect trend from last N candles. Returns 'UP', 'DOWN', or None."""
    if len(candles) < n + 1:
        return None

    recent = candles[-(n + 1):]
    ups = 0
    downs = 0
    for i in range(1, len(recent)):
        if recent[i].close > recent[i - 1].close:
            ups += 1
        elif recent[i].close < recent[i - 1].close:
            downs += 1

    if ups >= threshold:
        return "UP"
    elif downs >= threshold:
        return "DOWN"
    return None


def backtest_momentum(candles: List[Candle], config: MomentumConfig) -> List[Trade]:
    """Backtest momentum limit order strategy."""
    trades: List[Trade] = []

    # Active position tracking
    position = None  # (side, entry_price, entry_idx, size_usd)
    pending_limit = None  # (side, limit_price, placed_idx)

    for i in range(config.trend_candles + 1, len(candles) - 1):
        candle = candles[i]
        next_candle = candles[i + 1] if i + 1 < len(candles) else None

        # ── Check exits for open position ──
        if position is not None:
            side, entry_price, entry_idx, size_usd = position
            hold_candles = i - entry_idx

            exited = False
            exit_price = 0
            exit_reason = ""

            if side == "LONG":
                tp_price = entry_price * (1 + config.tp_bps / 10000)
                sl_price = entry_price * (1 - config.sl_bps / 10000)

                # Check if TP or SL hit during this candle
                if candle.high >= tp_price:
                    exit_price = tp_price
                    exit_reason = "TP"
                    exited = True
                elif candle.low <= sl_price:
                    exit_price = sl_price
                    exit_reason = "SL"
                    exited = True
                elif hold_candles >= 12:  # 1 hour max hold (12 x 5m)
                    exit_price = candle.close
                    exit_reason = "TIME"
                    exited = True

            elif side == "SHORT":
                tp_price = entry_price * (1 - config.tp_bps / 10000)
                sl_price = entry_price * (1 + config.sl_bps / 10000)

                if candle.low <= tp_price:
                    exit_price = tp_price
                    exit_reason = "TP"
                    exited = True
                elif candle.high >= sl_price:
                    exit_price = sl_price
                    exit_reason = "SL"
                    exited = True
                elif hold_candles >= 12:
                    exit_price = candle.close
                    exit_reason = "TIME"
                    exited = True

            if exited:
                if side == "LONG":
                    pnl_bps = (exit_price - entry_price) / entry_price * 10000
                else:
                    pnl_bps = (entry_price - exit_price) / entry_price * 10000
                pnl_usd = pnl_bps / 10000 * size_usd

                trades.append(Trade(
                    strategy="momentum",
                    symbol="",  # filled later
                    side=side,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    size_usd=size_usd,
                    entry_time=candles[entry_idx].time_str,
                    exit_time=candle.time_str,
                    exit_reason=exit_reason,
                    pnl_usd=pnl_usd,
                    pnl_bps=pnl_bps,
                    hold_candles=hold_candles
                ))
                position = None

        # ── Check if pending limit fills ──
        if pending_limit is not None and position is None:
            limit_side, limit_price, placed_idx = pending_limit

            # Check if current candle's range hits our limit
            filled = False
            if limit_side == "LONG" and candle.low <= limit_price:
                filled = True
            elif limit_side == "SHORT" and candle.high >= limit_price:
                filled = True

            if filled:
                position = (limit_side, limit_price, i, config.order_size_usd)
                pending_limit = None
            elif i - placed_idx >= 2:
                # Cancel if not filled within 2 candles (10 min)
                pending_limit = None

        # ── Place new limit if no position and no pending ──
        if position is None and pending_limit is None:
            trend = detect_trend(candles[:i + 1], config.trend_candles, config.trend_threshold)

            if trend == "DOWN":
                # Place SELL limit above current price
                limit_price = candle.close * (1 + config.offset_bps / 10000)
                pending_limit = ("SHORT", limit_price, i)

            elif trend == "UP":
                # Place BUY limit below current price
                limit_price = candle.close * (1 - config.offset_bps / 10000)
                pending_limit = ("LONG", limit_price, i)

    return trades


# ─── Grid MM Strategy ────────────────────────────────────────────

def backtest_grid_mm(candles: List[Candle], config: GridConfig) -> List[Trade]:
    """Backtest grid MM strategy."""
    trades: List[Trade] = []
    inventory = 0.0  # Net position in USD (positive = long, negative = short)

    # Track grid levels
    last_mid = None

    for i in range(1, len(candles)):
        candle = candles[i]
        prev = candles[i - 1]
        mid = (candle.open + candle.close) / 2

        if last_mid is None:
            last_mid = mid
            continue

        # Calculate ROC for dynamic spread
        roc_bps = abs(candle.close - prev.close) / prev.close * 10000

        # Dynamic spread (same as existing bots)
        if roc_bps > 50:
            last_mid = mid
            continue  # PAUSE
        elif roc_bps > 30:
            spread = 15
        elif roc_bps > 20:
            spread = 12
        elif roc_bps > 10:
            spread = 8
        elif roc_bps > 5:
            spread = 6
        else:
            spread = config.spread_bps  # Default

        # Grid levels
        for level in range(1, config.levels + 1):
            level_offset = spread * level
            buy_price = last_mid * (1 - level_offset / 10000)
            sell_price = last_mid * (1 + level_offset / 10000)

            # Check inventory limits
            max_inv = config.balance * config.max_inventory_pct

            # Check if buy fills (candle low touches buy level)
            if candle.low <= buy_price and inventory < max_inv:
                # Buy filled - now need to sell to close
                # Simulate: buy at buy_price, sell at next touch of sell_price
                # Simplified: assume round-trip within spread
                entry = buy_price
                # Check if sell side also fills in same candle
                if candle.high >= sell_price:
                    pnl_bps = (sell_price - buy_price) / buy_price * 10000
                    pnl_usd = pnl_bps / 10000 * config.order_size_usd
                    trades.append(Trade(
                        strategy="grid_mm",
                        symbol="",
                        side="LONG",
                        entry_price=buy_price,
                        exit_price=sell_price,
                        size_usd=config.order_size_usd,
                        entry_time=candle.time_str,
                        exit_time=candle.time_str,
                        exit_reason="SPREAD",
                        pnl_usd=pnl_usd,
                        pnl_bps=pnl_bps,
                        hold_candles=0
                    ))
                else:
                    # Buy fills but sell doesn't - adverse selection
                    # Price went down, close at candle close
                    exit_p = candle.close
                    pnl_bps = (exit_p - buy_price) / buy_price * 10000
                    pnl_usd = pnl_bps / 10000 * config.order_size_usd
                    trades.append(Trade(
                        strategy="grid_mm",
                        symbol="",
                        side="LONG",
                        entry_price=buy_price,
                        exit_price=exit_p,
                        size_usd=config.order_size_usd,
                        entry_time=candle.time_str,
                        exit_time=candle.time_str,
                        exit_reason="ADVERSE",
                        pnl_usd=pnl_usd,
                        pnl_bps=pnl_bps,
                        hold_candles=0
                    ))
                    inventory += config.order_size_usd

            # Check if sell fills
            if candle.high >= sell_price and inventory > -max_inv:
                entry = sell_price
                if candle.low <= buy_price:
                    # Both sides fill = spread captured (already counted above)
                    pass
                else:
                    exit_p = candle.close
                    pnl_bps = (sell_price - exit_p) / sell_price * 10000
                    pnl_usd = pnl_bps / 10000 * config.order_size_usd
                    trades.append(Trade(
                        strategy="grid_mm",
                        symbol="",
                        side="SHORT",
                        entry_price=sell_price,
                        exit_price=exit_p,
                        size_usd=config.order_size_usd,
                        entry_time=candle.time_str,
                        exit_time=candle.time_str,
                        exit_reason="ADVERSE",
                        pnl_usd=pnl_usd,
                        pnl_bps=pnl_bps,
                        hold_candles=0
                    ))
                    inventory -= config.order_size_usd

        # Decay inventory (simulate rebalance)
        inventory *= 0.95
        last_mid = mid

    return trades


# ─── Analysis ────────────────────────────────────────────────────

def analyze_trades(trades: List[Trade], label: str, hours: int):
    """Print detailed trade analysis."""
    if not trades:
        print(f"\n{'=' * 60}")
        print(f"{label}: NO TRADES")
        print(f"{'=' * 60}")
        return

    total_pnl = sum(t.pnl_usd for t in trades)
    total_volume = sum(t.size_usd for t in trades)
    wins = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0

    avg_win = sum(t.pnl_usd for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0
    avg_win_bps = sum(t.pnl_bps for t in wins) / len(wins) if wins else 0
    avg_loss_bps = sum(t.pnl_bps for t in losses) / len(losses) if losses else 0

    # By exit reason
    exit_reasons = {}
    for t in trades:
        if t.exit_reason not in exit_reasons:
            exit_reasons[t.exit_reason] = {'count': 0, 'pnl': 0}
        exit_reasons[t.exit_reason]['count'] += 1
        exit_reasons[t.exit_reason]['pnl'] += t.pnl_usd

    # By side
    longs = [t for t in trades if t.side == "LONG"]
    shorts = [t for t in trades if t.side == "SHORT"]
    long_pnl = sum(t.pnl_usd for t in longs)
    short_pnl = sum(t.pnl_usd for t in shorts)
    long_wr = len([t for t in longs if t.pnl_usd > 0]) / len(longs) * 100 if longs else 0
    short_wr = len([t for t in shorts if t.pnl_usd > 0]) / len(shorts) * 100 if shorts else 0

    # Sharpe-like metric (PnL consistency)
    pnls = [t.pnl_usd for t in trades]
    avg_pnl = sum(pnls) / len(pnls)
    variance = sum((p - avg_pnl) ** 2 for p in pnls) / len(pnls)
    std_pnl = variance ** 0.5
    sharpe = avg_pnl / std_pnl if std_pnl > 0 else 0

    # Max drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cumulative += t.pnl_usd
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd

    # Volume per hour
    vol_per_hr = total_volume / hours if hours > 0 else 0

    print(f"\n{'=' * 60}")
    print(f"{label}")
    print(f"{'=' * 60}")
    print(f"  Trades:      {len(trades)} ({len(trades)/hours*24:.0f}/day)")
    print(f"  Win Rate:    {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"  Net P&L:     ${total_pnl:+.2f}")
    print(f"  Volume:      ${total_volume:,.0f} (${vol_per_hr:,.0f}/hr)")
    print(f"  Avg Win:     ${avg_win:+.2f} ({avg_win_bps:+.1f} bps)")
    print(f"  Avg Loss:    ${avg_loss:+.2f} ({avg_loss_bps:+.1f} bps)")
    print(f"  Profit Factor: {abs(sum(t.pnl_usd for t in wins)) / abs(sum(t.pnl_usd for t in losses)):.2f}" if losses and sum(t.pnl_usd for t in losses) != 0 else "  Profit Factor: inf")
    print(f"  Sharpe:      {sharpe:.3f}")
    print(f"  Max Drawdown: ${max_dd:.2f}")
    print(f"  Best Trade:  ${max(t.pnl_usd for t in trades):+.2f}")
    print(f"  Worst Trade: ${min(t.pnl_usd for t in trades):+.2f}")

    print(f"\n  BY SIDE:")
    print(f"    LONG:  {len(longs)} trades | WR: {long_wr:.0f}% | P&L: ${long_pnl:+.2f}")
    print(f"    SHORT: {len(shorts)} trades | WR: {short_wr:.0f}% | P&L: ${short_pnl:+.2f}")

    print(f"\n  BY EXIT REASON:")
    for reason, data in sorted(exit_reasons.items(), key=lambda x: x[1]['pnl'], reverse=True):
        print(f"    {reason:>8}: {data['count']:>4} trades | P&L: ${data['pnl']:+.2f}")

    # Equity curve summary (first/mid/last)
    cum = 0
    checkpoints = [len(trades) // 4, len(trades) // 2, 3 * len(trades) // 4, len(trades) - 1]
    print(f"\n  EQUITY CURVE:")
    for idx in checkpoints:
        cum = sum(t.pnl_usd for t in trades[:idx + 1])
        print(f"    Trade #{idx + 1}: ${cum:+.2f}")


def compare_strategies(momentum_trades: List[Trade], grid_trades: List[Trade], hours: int):
    """Side-by-side comparison."""
    m_pnl = sum(t.pnl_usd for t in momentum_trades)
    g_pnl = sum(t.pnl_usd for t in grid_trades)
    m_vol = sum(t.size_usd for t in momentum_trades)
    g_vol = sum(t.size_usd for t in grid_trades)
    m_wr = len([t for t in momentum_trades if t.pnl_usd > 0]) / len(momentum_trades) * 100 if momentum_trades else 0
    g_wr = len([t for t in grid_trades if t.pnl_usd > 0]) / len(grid_trades) * 100 if grid_trades else 0

    print(f"\n{'=' * 60}")
    print(f"HEAD-TO-HEAD COMPARISON ({hours}h)")
    print(f"{'=' * 60}")
    print(f"  {'Metric':<20} {'Momentum':>15} {'Grid MM':>15}")
    print(f"  {'-' * 50}")
    print(f"  {'Trades':<20} {len(momentum_trades):>15} {len(grid_trades):>15}")
    print(f"  {'Win Rate':<20} {m_wr:>14.1f}% {g_wr:>14.1f}%")
    print(f"  {'Net P&L':<20} {'${:+.2f}'.format(m_pnl):>15} {'${:+.2f}'.format(g_pnl):>15}")
    print(f"  {'Volume':<20} {'${:,.0f}'.format(m_vol):>15} {'${:,.0f}'.format(g_vol):>15}")
    print(f"  {'P&L/Volume':<20} {m_pnl/m_vol*10000 if m_vol else 0:>13.1f}bp {g_pnl/g_vol*10000 if g_vol else 0:>13.1f}bp")
    print(f"  {'Vol/Hour':<20} {'${:,.0f}'.format(m_vol/hours):>15} {'${:,.0f}'.format(g_vol/hours):>15}")

    winner = "MOMENTUM" if m_pnl > g_pnl else "GRID MM"
    print(f"\n  WINNER: {winner} (by ${abs(m_pnl - g_pnl):.2f})")


# ─── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Momentum vs Grid MM Backtest")
    parser.add_argument("--hours", type=int, default=72, help="Backtest period in hours")
    parser.add_argument("--symbol", type=str, default="BTCUSDT", help="Trading symbol")
    parser.add_argument("--offset", type=float, default=7.0, help="Momentum offset in bps")
    parser.add_argument("--tp", type=float, default=15.0, help="Take profit in bps")
    parser.add_argument("--sl", type=float, default=10.0, help="Stop loss in bps")
    parser.add_argument("--spread", type=float, default=15.0, help="Grid MM spread in bps")
    parser.add_argument("--size", type=float, default=100.0, help="Order size in USD")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    # Fetch data
    raw = fetch_klines(args.symbol, "5m", args.hours)
    candles = parse_klines(raw)

    if len(candles) < 20:
        print("ERROR: Not enough data")
        return

    # Price context
    start_price = candles[0].close
    end_price = candles[-1].close
    price_change = (end_price - start_price) / start_price * 100
    high = max(c.high for c in candles)
    low = min(c.low for c in candles)
    range_pct = (high - low) / low * 100

    # Volatility profile
    rocs = [abs(candles[i].close - candles[i-1].close) / candles[i-1].close * 10000
            for i in range(1, len(candles))]
    avg_roc = sum(rocs) / len(rocs)

    # Choppiness
    reversals = 0
    for i in range(2, len(candles)):
        if (candles[i].close > candles[i-1].close) != (candles[i-1].close > candles[i-2].close):
            reversals += 1
    choppiness = reversals / (len(candles) - 2) * 100

    print(f"\n{'=' * 60}")
    print(f"BACKTEST: {args.symbol} | {args.hours}h | {len(candles)} candles")
    print(f"{'=' * 60}")
    print(f"  Period:      {candles[0].time_str} → {candles[-1].time_str}")
    print(f"  Price:       ${start_price:,.2f} → ${end_price:,.2f} ({price_change:+.1f}%)")
    print(f"  Range:       ${low:,.2f} - ${high:,.2f} ({range_pct:.1f}%)")
    print(f"  Avg 5m ROC:  {avg_roc:.1f} bps")
    print(f"  Choppiness:  {choppiness:.0f}%")

    market_type = "CHOPPY" if choppiness > 55 else "TRENDING" if choppiness < 40 else "MIXED"
    vol_level = "HIGH" if avg_roc > 15 else "MODERATE" if avg_roc > 8 else "LOW"
    print(f"  Market Type: {market_type} + {vol_level} VOL")

    # Run backtests
    m_config = MomentumConfig(
        offset_bps=args.offset,
        tp_bps=args.tp,
        sl_bps=args.sl,
        order_size_usd=args.size
    )

    g_config = GridConfig(
        spread_bps=args.spread,
        order_size_usd=args.size,
        levels=2,
        balance=80.0
    )

    momentum_trades = backtest_momentum(candles, m_config)
    grid_trades = backtest_grid_mm(candles, g_config)

    # Tag symbol
    for t in momentum_trades:
        t.symbol = args.symbol
    for t in grid_trades:
        t.symbol = args.symbol

    # Print results
    analyze_trades(momentum_trades, f"MOMENTUM LIMITS ({args.offset} bps offset, {args.tp}/{args.sl} TP/SL)", args.hours)
    analyze_trades(grid_trades, f"GRID MM ({args.spread} bps spread, {g_config.levels} levels)", args.hours)
    compare_strategies(momentum_trades, grid_trades, args.hours)

    # Segment analysis - split into chunks to see consistency
    if args.hours >= 48:
        chunk_hours = 24
        chunk_candles = chunk_hours * 12
        n_chunks = len(candles) // chunk_candles

        print(f"\n{'=' * 60}")
        print(f"DAILY BREAKDOWN")
        print(f"{'=' * 60}")
        print(f"  {'Day':<8} {'M_Trades':>8} {'M_P&L':>10} {'G_Trades':>8} {'G_P&L':>10} {'Winner':>10}")
        print(f"  {'-' * 56}")

        for chunk in range(n_chunks):
            start_idx = chunk * chunk_candles
            end_idx = min(start_idx + chunk_candles, len(candles))
            chunk_data = candles[start_idx:end_idx]

            m_chunk = backtest_momentum(chunk_data, m_config)
            g_chunk = backtest_grid_mm(chunk_data, g_config)

            m_p = sum(t.pnl_usd for t in m_chunk)
            g_p = sum(t.pnl_usd for t in g_chunk)
            w = "MOMENTUM" if m_p > g_p else "GRID" if g_p > m_p else "TIE"

            print(f"  Day {chunk+1:<4} {len(m_chunk):>8} ${m_p:>+9.2f} {len(g_chunk):>8} ${g_p:>+9.2f} {w:>10}")

    # JSON output
    if args.json:
        results = {
            'symbol': args.symbol,
            'hours': args.hours,
            'market': {
                'start_price': start_price,
                'end_price': end_price,
                'change_pct': price_change,
                'avg_roc_bps': avg_roc,
                'choppiness': choppiness,
            },
            'momentum': {
                'trades': len(momentum_trades),
                'pnl': sum(t.pnl_usd for t in momentum_trades),
                'volume': sum(t.size_usd for t in momentum_trades),
                'win_rate': len([t for t in momentum_trades if t.pnl_usd > 0]) / len(momentum_trades) * 100 if momentum_trades else 0,
            },
            'grid': {
                'trades': len(grid_trades),
                'pnl': sum(t.pnl_usd for t in grid_trades),
                'volume': sum(t.size_usd for t in grid_trades),
                'win_rate': len([t for t in grid_trades if t.pnl_usd > 0]) / len(grid_trades) * 100 if grid_trades else 0,
            }
        }
        print(f"\n{json.dumps(results, indent=2)}")


if __name__ == "__main__":
    main()
