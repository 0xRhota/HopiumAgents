#!/usr/bin/env python3
"""
Strategy Backtest — High Volume + Positive PnL
================================================
Pulls 30 days of 15m candles from Binance for all symbols.
Runs v9 scoring engine over every candle.
Simulates 6 strategies with different TP/SL/thresholds.
Measures volume + PnL for each.

Run: python3 scripts/strategy_backtest.py
Output: research/mcp_backtest_results/strategy_backtest_results.json
"""

import json
import time
import requests
import numpy as np
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import defaultdict

RESULTS_DIR = Path("research/mcp_backtest_results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

BINANCE_URL = "https://api.binance.com/api/v3/klines"

# All symbols we trade — Nado (20) + Hibachi (6 overlap)
SYMBOLS = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "XRP": "XRPUSDT", "BNB": "BNBUSDT", "SUI": "SUIUSDT",
    "ZEC": "ZECUSDT", "AAVE": "AAVEUSDT", "TAO": "TAOUSDT",
    "XMR": "XMRUSDT", "LIT": "LITUSDT", "UNI": "UNIUSDT",
    "DOGE": "DOGEUSDT", "PENGU": "PENGUUSDT",
    "PUMP": "PUMPUSDT", "ASTER": "ASTERUSDT",
    "XPL": "XPLUSDT", "WLFI": "WLFIUSDT",
}

HIBACHI_SYMBOLS = ["BTC", "ETH", "SOL", "SUI", "XRP", "BNB"]
NADO_SYMBOLS = list(SYMBOLS.keys())


# ── Data Fetching ───────────────────────────────────────────────────
def fetch_candles(symbol, pair, days=30, interval="15m"):
    """Fetch historical candles from Binance in chunks."""
    all_candles = []
    end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_time = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    current = start_time
    while current < end_time:
        try:
            resp = requests.get(BINANCE_URL, params={
                "symbol": pair, "interval": interval,
                "startTime": current, "limit": 1000
            }, timeout=15)
            if resp.status_code != 200:
                print(f"  {symbol}: HTTP {resp.status_code}, skipping")
                return None
            data = resp.json()
            if not data:
                break
            all_candles.extend(data)
            current = data[-1][0] + 1  # next ms after last candle
            time.sleep(0.1)  # rate limit
        except Exception as e:
            print(f"  {symbol}: error {e}")
            return None

    if not all_candles:
        return None

    # Parse into structured format
    candles = []
    for k in all_candles:
        candles.append({
            "ts": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return candles


# ── V9 Scoring Engine (simplified) ─────────────────────────────────
def calc_ema(data, period):
    if len(data) < period:
        return None
    ema = [data[0]]
    k = 2 / (period + 1)
    for d in data[1:]:
        ema.append(d * k + ema[-1] * (1 - k))
    return ema


def score_candle(candles, idx):
    """Score a single candle position using v9 5-signal system.
    Returns (score, direction, details) or None if insufficient data."""
    if idx < 30:
        return None

    window = candles[max(0, idx - 50):idx + 1]
    closes = [c["close"] for c in window]
    highs = [c["high"] for c in window]
    lows = [c["low"] for c in window]
    volumes = [c["volume"] for c in window]

    if len(closes) < 30:
        return None

    # 1. RSI(14)
    deltas = np.diff(closes[-15:])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains) if len(gains) > 0 else 0
    avg_loss = np.mean(losses) if len(losses) > 0 else 0
    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    rsi_score = 0.0
    rsi_dir = "NONE"
    if rsi < 35 or rsi > 65:
        rsi_score = 1.0
    elif rsi < 40 or rsi > 60:
        rsi_score = 0.7
    elif rsi < 45 or rsi > 55:
        rsi_score = 0.3

    if rsi < 45:
        rsi_dir = "LONG"
    elif rsi > 55:
        rsi_dir = "SHORT"

    # 2. MACD(12,26,9)
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if not ema12 or not ema26 or len(ema12) < 26:
        macd_score = 0.0
        macd_dir = "NONE"
    else:
        # Align lengths
        diff = len(ema12) - len(ema26)
        macd_line = [ema12[i + diff] - ema26[i] for i in range(len(ema26))]
        signal_line = calc_ema(macd_line, 9)

        if signal_line and len(signal_line) >= 2:
            hist = macd_line[-1] - signal_line[-1]
            prev_hist = macd_line[-2] - signal_line[-2]

            if (macd_line[-1] > signal_line[-1]) != (macd_line[-2] > signal_line[-2]):
                macd_score = 1.0  # crossover
            elif abs(hist) > abs(prev_hist) and abs(hist) > abs(macd_line[-3] - signal_line[-3] if len(signal_line) >= 3 else 0):
                macd_score = 0.8
            elif abs(hist) > abs(prev_hist):
                macd_score = 0.5
            else:
                macd_score = 0.0

            macd_dir = "LONG" if macd_line[-1] > signal_line[-1] else "SHORT"
        else:
            macd_score = 0.0
            macd_dir = "NONE"

    # 3. Volume
    vol_avg = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
    vol_ratio = volumes[-1] / vol_avg if vol_avg > 0 else 0

    if vol_ratio >= 2.0:
        vol_score = 1.0
    elif vol_ratio >= 1.5:
        vol_score = 0.7
    elif vol_ratio >= 1.2:
        vol_score = 0.4
    else:
        vol_score = 0.0

    # 4. Price Action (position in 20-candle range)
    recent_high = max(highs[-20:])
    recent_low = min(lows[-20:])
    price_range = recent_high - recent_low
    if price_range > 0:
        position = (closes[-1] - recent_low) / price_range
    else:
        position = 0.5

    pa_score = 0.0
    pa_dir = "NONE"
    if position < 0.15 or position > 0.85:
        pa_score = 1.0
    elif position < 0.25 or position > 0.75:
        pa_score = 0.7
    elif position < 0.35 or position > 0.65:
        pa_score = 0.4

    if position < 0.35:
        pa_dir = "LONG"
    elif position > 0.65:
        pa_dir = "SHORT"

    # 5. EMA 8/21
    ema8 = calc_ema(closes, 8)
    ema21 = calc_ema(closes, 21)
    if ema8 and ema21:
        diff_bps = (ema8[-1] - ema21[-1]) / closes[-1] * 10000
        ema8_slope = (ema8[-1] - ema8[-4]) / closes[-1] * 10000 if len(ema8) >= 4 else 0

        if (ema8[-1] > ema21[-1]) != (ema8[-2] > ema21[-2]):
            ema_score = 1.0
        elif abs(diff_bps) > 20 and abs(ema8_slope) > 10:
            ema_score = 0.8
        elif abs(diff_bps) > 10:
            ema_score = 0.5
        else:
            ema_score = 0.0

        ema_dir = "LONG" if ema8[-1] > ema21[-1] else "SHORT"
    else:
        ema_score = 0.0
        ema_dir = "NONE"

    total_score = rsi_score + macd_score + vol_score + pa_score + ema_score

    # Direction by confluence
    dirs = [d for d in [rsi_dir, macd_dir, pa_dir, ema_dir] if d != "NONE"]
    if not dirs:
        direction = "NONE"
    else:
        long_count = dirs.count("LONG")
        short_count = dirs.count("SHORT")
        if long_count > short_count:
            direction = "LONG"
        elif short_count > long_count:
            direction = "SHORT"
        else:
            # Tie-break: RSI + MACD
            if rsi_dir == macd_dir and rsi_dir != "NONE":
                direction = rsi_dir
            else:
                direction = "NONE"

    return {
        "score": round(total_score, 2),
        "direction": direction,
        "rsi": round(rsi, 1),
        "vol_ratio": round(vol_ratio, 2),
        "rsi_score": rsi_score,
        "macd_score": macd_score,
        "vol_score": vol_score,
        "pa_score": pa_score,
        "ema_score": ema_score,
    }


# ── Strategy Simulator ──────────────────────────────────────────────
def simulate_strategy(candles_by_symbol, config, symbol_list):
    """Simulate a strategy across all symbols.

    config keys:
        name, score_min, tp_bps, sl_bps, max_hold_candles,
        max_positions, size_usd, require_volume, direction_filter,
        cancel_after_candles
    """
    trades = []
    open_positions = []  # list of {symbol, side, entry_price, entry_idx, size_usd}

    # Flatten all candles with symbol tag, sort by timestamp
    all_events = []
    for sym in symbol_list:
        if sym not in candles_by_symbol:
            continue
        for i, c in enumerate(candles_by_symbol[sym]):
            all_events.append((c["ts"], sym, i))

    all_events.sort(key=lambda x: x[0])

    # Track per-symbol state
    last_scored = {}  # sym -> (idx, signal)
    pending_orders = []  # {symbol, side, limit_price, placed_idx, placed_ts}

    score_min = config["score_min"]
    tp_bps = config["tp_bps"]
    sl_bps = config["sl_bps"]
    max_hold = config.get("max_hold_candles", 999999)
    max_pos = config["max_positions"]
    require_vol = config.get("require_volume", True)
    dir_filter = config.get("direction_filter", None)  # {symbol: "LONG"/"SHORT"/None}
    cancel_after = config.get("cancel_after_candles", 2)
    offset_bps = config.get("offset_bps", 8)

    # Process candle by candle
    processed_ts = set()

    for ts, sym, idx in all_events:
        candles = candles_by_symbol[sym]

        # Check pending orders for fill
        new_pending = []
        for order in pending_orders:
            if order["symbol"] != sym:
                new_pending.append(order)
                continue

            candle = candles[idx]
            filled = False

            # Check if limit price was hit
            if order["side"] == "LONG" and candle["low"] <= order["limit_price"]:
                filled = True
                fill_price = order["limit_price"]
            elif order["side"] == "SHORT" and candle["high"] >= order["limit_price"]:
                filled = True
                fill_price = order["limit_price"]

            # Check expiry
            elapsed = idx - order["placed_idx"]
            if elapsed >= cancel_after and not filled:
                continue  # expired, drop it

            if filled:
                open_positions.append({
                    "symbol": sym, "side": order["side"],
                    "entry_price": fill_price, "entry_idx": idx,
                    "entry_ts": candle["ts"],
                    "size_usd": config["size_usd"],
                })
            else:
                new_pending.append(order)

        pending_orders = new_pending

        # Check open positions for TP/SL/time exit
        still_open = []
        for pos in open_positions:
            if pos["symbol"] != sym:
                still_open.append(pos)
                continue

            candle = candles[idx]
            entry = pos["entry_price"]
            hold_candles = idx - pos["entry_idx"]

            if pos["side"] == "LONG":
                tp_price = entry * (1 + tp_bps / 10000)
                sl_price = entry * (1 - sl_bps / 10000)
                hit_tp = candle["high"] >= tp_price
                hit_sl = candle["low"] <= sl_price
            else:
                tp_price = entry * (1 - tp_bps / 10000)
                sl_price = entry * (1 + sl_bps / 10000)
                hit_tp = candle["low"] <= tp_price
                hit_sl = candle["high"] >= sl_price

            exit_reason = None
            exit_price = None

            if hit_tp and hit_sl:
                # Both hit in same candle — assume SL hit first (conservative)
                exit_reason = "SL"
                exit_price = sl_price
            elif hit_tp:
                exit_reason = "TP"
                exit_price = tp_price
            elif hit_sl:
                exit_reason = "SL"
                exit_price = sl_price
            elif hold_candles >= max_hold:
                exit_reason = "TIME"
                exit_price = candle["close"]

            if exit_reason:
                if pos["side"] == "LONG":
                    pnl = (exit_price - entry) / entry * pos["size_usd"]
                else:
                    pnl = (entry - exit_price) / entry * pos["size_usd"]

                trades.append({
                    "symbol": sym, "side": pos["side"],
                    "entry_price": entry, "exit_price": exit_price,
                    "pnl": round(pnl, 4),
                    "exit_reason": exit_reason,
                    "hold_candles": hold_candles,
                    "hold_minutes": hold_candles * 15,
                    "size_usd": pos["size_usd"],
                    "volume": pos["size_usd"] * 2,  # round trip
                })
            else:
                still_open.append(pos)

        open_positions = still_open

        # Score current candle for entry signals
        signal = score_candle(candles, idx)
        if not signal:
            continue

        if signal["score"] < score_min:
            continue

        if signal["direction"] == "NONE":
            continue

        if require_vol and signal["vol_score"] == 0:
            continue

        # Direction filter
        if dir_filter and sym in dir_filter:
            if dir_filter[sym] and signal["direction"] != dir_filter[sym]:
                continue

        # Position limit
        current_pos_count = len([p for p in open_positions]) + len([o for o in pending_orders])
        if current_pos_count >= max_pos:
            continue

        # Already have position in this symbol?
        if any(p["symbol"] == sym for p in open_positions):
            continue
        if any(o["symbol"] == sym for o in pending_orders):
            continue

        # Place limit order
        price = candles[idx]["close"]
        if signal["direction"] == "LONG":
            limit_price = price * (1 - offset_bps / 10000)
        else:
            limit_price = price * (1 + offset_bps / 10000)

        pending_orders.append({
            "symbol": sym, "side": signal["direction"],
            "limit_price": limit_price,
            "placed_idx": idx, "placed_ts": ts,
        })

    return trades


# ── Strategy Configs ────────────────────────────────────────────────
STRATEGIES = {
    "A_fast_scalp": {
        "name": "Fast Scalp + Tight TP/SL",
        "score_min": 2.5,
        "tp_bps": 80,   # 0.8%
        "sl_bps": 40,   # 0.4%
        "max_hold_candles": 8,   # 2 hours
        "max_positions": 5,
        "size_usd": 100,
        "require_volume": False,
        "offset_bps": 5,
        "cancel_after_candles": 1,
    },
    "B_baseline_v9": {
        "name": "Current v9 (baseline)",
        "score_min": 3.0,
        "tp_bps": 150,  # 1.5%
        "sl_bps": 100,  # 1.0%
        "max_hold_candles": 96,  # 24 hours
        "max_positions": 5,
        "size_usd": 100,
        "require_volume": True,
        "offset_bps": 8,
        "cancel_after_candles": 2,
    },
    "C_asymmetric_rr": {
        "name": "Asymmetric R:R Volume Machine",
        "score_min": 2.5,
        "tp_bps": 150,  # 1.5%
        "sl_bps": 50,   # 0.5%  → 3:1 R:R
        "max_hold_candles": 16,  # 4 hours
        "max_positions": 5,
        "size_usd": 100,
        "require_volume": False,
        "offset_bps": 5,
        "cancel_after_candles": 2,
    },
    "D_tight_recycler": {
        "name": "Tight TP Recycler",
        "score_min": 2.5,
        "tp_bps": 50,   # 0.5% — take small wins fast
        "sl_bps": 30,   # 0.3%
        "max_hold_candles": 4,   # 1 hour
        "max_positions": 5,
        "size_usd": 100,
        "require_volume": False,
        "offset_bps": 3,
        "cancel_after_candles": 1,
    },
    "E_volume_king": {
        "name": "Volume King (lowest threshold)",
        "score_min": 2.0,
        "tp_bps": 60,   # 0.6%
        "sl_bps": 40,   # 0.4%  → 1.5:1 R:R
        "max_hold_candles": 6,   # 1.5 hours
        "max_positions": 5,
        "size_usd": 100,
        "require_volume": False,
        "offset_bps": 3,
        "cancel_after_candles": 1,
    },
    "F_patient_winner": {
        "name": "Patient Winner (long holds)",
        "score_min": 3.0,
        "tp_bps": 200,  # 2.0%
        "sl_bps": 150,  # 1.5%
        "max_hold_candles": 192, # 48 hours
        "max_positions": 3,
        "size_usd": 100,
        "require_volume": True,
        "offset_bps": 8,
        "cancel_after_candles": 4,
    },
}


# ── Main ────────────────────────────────────────────────────────────
def main():
    print("=" * 70)
    print("STRATEGY BACKTEST — High Volume + Positive PnL")
    print("=" * 70)

    # Phase 1: Fetch candles
    print(f"\nFetching 30 days of 15m candles for {len(SYMBOLS)} symbols...")
    candles_by_symbol = {}
    for sym, pair in SYMBOLS.items():
        print(f"  {sym} ({pair})...", end=" ", flush=True)
        candles = fetch_candles(sym, pair, days=30, interval="15m")
        if candles:
            candles_by_symbol[sym] = candles
            days_covered = (candles[-1]["ts"] - candles[0]["ts"]) / (1000 * 86400)
            print(f"{len(candles)} candles ({days_covered:.1f} days)")
        else:
            print("FAILED")
        time.sleep(0.2)

    print(f"\nLoaded data for {len(candles_by_symbol)} symbols")

    # Save candle data for reuse
    cache_file = RESULTS_DIR / "candle_cache.json"
    with open(cache_file, "w") as f:
        json.dump({k: v for k, v in candles_by_symbol.items()}, f)
    print(f"Cached to {cache_file}")

    # Phase 2: Run each strategy
    print(f"\nRunning {len(STRATEGIES)} strategies...")
    results = {}

    for strat_id, config in STRATEGIES.items():
        print(f"\n{'─' * 50}")
        print(f"Strategy {strat_id}: {config['name']}")
        print(f"  score_min={config['score_min']} TP={config['tp_bps']}bps SL={config['sl_bps']}bps max_hold={config['max_hold_candles']*15}min")

        # Run on all symbols (simulates Nado-like setup)
        trades = simulate_strategy(candles_by_symbol, config, list(candles_by_symbol.keys()))

        if not trades:
            print("  NO TRADES")
            results[strat_id] = {"trades": 0}
            continue

        total_pnl = sum(t["pnl"] for t in trades)
        winners = [t for t in trades if t["pnl"] > 0]
        losers = [t for t in trades if t["pnl"] <= 0]
        wr = len(winners) / len(trades) * 100
        total_volume = sum(t["volume"] for t in trades)

        # Time span
        first_candle_ts = min(candles_by_symbol[s][0]["ts"] for s in candles_by_symbol)
        last_candle_ts = max(candles_by_symbol[s][-1]["ts"] for s in candles_by_symbol)
        days = (last_candle_ts - first_candle_ts) / (1000 * 86400)

        daily_volume = total_volume / days if days > 0 else 0
        daily_trades = len(trades) / days if days > 0 else 0
        daily_pnl = total_pnl / days if days > 0 else 0

        avg_win = np.mean([t["pnl"] for t in winners]) if winners else 0
        avg_loss = np.mean([t["pnl"] for t in losers]) if losers else 0
        rr_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 999

        # By exit reason
        by_reason = defaultdict(lambda: {"count": 0, "pnl": 0})
        for t in trades:
            by_reason[t["exit_reason"]]["count"] += 1
            by_reason[t["exit_reason"]]["pnl"] += t["pnl"]

        # Max drawdown
        running_pnl = 0
        peak = 0
        max_dd = 0
        for t in trades:
            running_pnl += t["pnl"]
            peak = max(peak, running_pnl)
            dd = peak - running_pnl
            max_dd = max(max_dd, dd)

        print(f"  Trades: {len(trades)} ({daily_trades:.1f}/day)")
        print(f"  Volume: ${total_volume:,.0f} (${daily_volume:,.0f}/day)")
        print(f"  PnL: ${total_pnl:+.2f} (${daily_pnl:+.2f}/day)")
        print(f"  WR: {wr:.1f}% | Avg Win: ${avg_win:+.4f} | Avg Loss: ${avg_loss:+.4f} | R:R: {rr_ratio:.2f}")
        print(f"  Max Drawdown: ${max_dd:.2f}")
        for reason, stats in sorted(by_reason.items()):
            print(f"    {reason}: {stats['count']} trades, ${stats['pnl']:+.2f}")

        results[strat_id] = {
            "name": config["name"],
            "config": {k: v for k, v in config.items() if k != "direction_filter"},
            "trades": len(trades),
            "daily_trades": round(daily_trades, 1),
            "total_volume": round(total_volume, 2),
            "daily_volume": round(daily_volume, 2),
            "total_pnl": round(total_pnl, 4),
            "daily_pnl": round(daily_pnl, 4),
            "win_rate": round(wr, 1),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "rr_ratio": round(rr_ratio, 2),
            "max_drawdown": round(max_dd, 2),
            "by_reason": {k: dict(v) for k, v in by_reason.items()},
            "days": round(days, 1),
        }

    # Phase 3: Summary comparison
    print(f"\n{'=' * 70}")
    print("STRATEGY COMPARISON")
    print(f"{'=' * 70}")
    print(f"{'Strategy':<30} {'Trades':>7} {'Trades/d':>9} {'Vol/day':>10} {'PnL':>10} {'PnL/day':>9} {'WR':>6} {'R:R':>5} {'MaxDD':>8}")
    print("-" * 100)

    for sid, r in sorted(results.items(), key=lambda x: x[1].get("daily_pnl", -999), reverse=True):
        if r["trades"] == 0:
            continue
        print(f"  {r.get('name', sid):<28} {r['trades']:>7} {r['daily_trades']:>8.1f} "
              f"${r['daily_volume']:>9,.0f} ${r['total_pnl']:>+9.2f} ${r['daily_pnl']:>+8.2f} "
              f"{r['win_rate']:>5.1f}% {r['rr_ratio']:>5.2f} ${r['max_drawdown']:>7.2f}")

    print(f"\n{'=' * 70}")

    # Save results
    results_file = RESULTS_DIR / "strategy_backtest_results.json"
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {results_file}")


if __name__ == "__main__":
    import os
    os.chdir(Path(__file__).parent.parent)
    main()
