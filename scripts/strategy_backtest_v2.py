#!/usr/bin/env python3
"""
Strategy Backtest v2 — Research-Backed Strategies
===================================================
Adds: MACD(8,17,9), RSI(7), Bollinger Bands, partial TP,
      direction bias, adaptive scalper from web research.

Uses cached candle data from v1 run.
Run: python3 scripts/strategy_backtest_v2.py
"""

import json
import numpy as np
from pathlib import Path
from collections import defaultdict

RESULTS_DIR = Path("research/mcp_backtest_results")
CACHE_FILE = RESULTS_DIR / "candle_cache.json"


# ── Indicator Library ───────────────────────────────────────────────
def ema(data, period):
    if len(data) < period:
        return None
    out = [data[0]]
    k = 2 / (period + 1)
    for d in data[1:]:
        out.append(d * k + out[-1] * (1 - k))
    return out


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = np.diff(closes[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains)
    avg_loss = np.mean(losses)
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    if not ema_fast or not ema_slow:
        return None, None, None
    diff = len(ema_fast) - len(ema_slow)
    macd_line = [ema_fast[i + diff] - ema_slow[i] for i in range(len(ema_slow))]
    sig = ema(macd_line, signal)
    if not sig or len(sig) < 2:
        return None, None, None
    hist = macd_line[-1] - sig[-1]
    return macd_line[-1], sig[-1], hist


def bollinger(closes, period=20, std_dev=2.5):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = np.mean(window)
    std = np.std(window)
    return mid + std_dev * std, mid, mid - std_dev * std


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    return np.mean(trs[-period:])


# ── Scoring Engines ─────────────────────────────────────────────────
def score_v9_original(candles, idx):
    """Original v9: MACD(12,26,9), RSI(14)"""
    if idx < 30:
        return None
    w = candles[max(0, idx-50):idx+1]
    closes = [c["close"] for c in w]
    highs = [c["high"] for c in w]
    lows = [c["low"] for c in w]
    volumes = [c["volume"] for c in w]
    if len(closes) < 30:
        return None

    r = rsi(closes, 14)
    if r is None:
        return None

    # RSI score
    rs = 0.0
    rd = "NONE"
    if r < 35 or r > 65: rs = 1.0
    elif r < 40 or r > 60: rs = 0.7
    elif r < 45 or r > 55: rs = 0.3
    if r < 45: rd = "LONG"
    elif r > 55: rd = "SHORT"

    # MACD(12,26,9)
    ml, ms_line, mh = macd(closes, 12, 26, 9)
    mc = 0.0
    md = "NONE"
    if ml is not None and ms_line is not None:
        prev_ml, prev_ms, _ = macd(closes[:-1], 12, 26, 9) if len(closes) > 1 else (None, None, None)
        if prev_ml is not None:
            if (ml > ms_line) != (prev_ml > prev_ms):
                mc = 1.0
            elif mh is not None and _ is not None and abs(mh) > abs(_):
                mc = 0.5
        md = "LONG" if ml > ms_line else "SHORT"

    # Volume
    va = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
    vr = volumes[-1] / va if va > 0 else 0
    vs = 1.0 if vr >= 2.0 else 0.7 if vr >= 1.5 else 0.4 if vr >= 1.2 else 0.0

    # Price action
    rh = max(highs[-20:])
    rl = min(lows[-20:])
    rng = rh - rl
    pos = (closes[-1] - rl) / rng if rng > 0 else 0.5
    ps = 1.0 if pos < 0.15 or pos > 0.85 else 0.7 if pos < 0.25 or pos > 0.75 else 0.4 if pos < 0.35 or pos > 0.65 else 0.0
    pd = "LONG" if pos < 0.35 else "SHORT" if pos > 0.65 else "NONE"

    # EMA 8/21
    e8 = ema(closes, 8)
    e21 = ema(closes, 21)
    es = 0.0
    ed = "NONE"
    if e8 and e21:
        db = (e8[-1] - e21[-1]) / closes[-1] * 10000
        if len(e8) >= 2 and len(e21) >= 2:
            if (e8[-1] > e21[-1]) != (e8[-2] > e21[-2]):
                es = 1.0
            elif abs(db) > 20:
                es = 0.8
            elif abs(db) > 10:
                es = 0.5
        ed = "LONG" if e8[-1] > e21[-1] else "SHORT"

    total = rs + mc + vs + ps + es
    dirs = [d for d in [rd, md, pd, ed] if d != "NONE"]
    lc = dirs.count("LONG")
    sc = dirs.count("SHORT")
    direction = "LONG" if lc > sc else "SHORT" if sc > lc else "NONE"

    return {"score": round(total, 2), "direction": direction, "rsi": r, "vol_ratio": vr}


def score_research_tuned(candles, idx):
    """Research-backed: MACD(8,17,9), RSI(7), BB(20,2.5)"""
    if idx < 30:
        return None
    w = candles[max(0, idx-50):idx+1]
    closes = [c["close"] for c in w]
    highs = [c["high"] for c in w]
    lows = [c["low"] for c in w]
    volumes = [c["volume"] for c in w]
    if len(closes) < 30:
        return None

    # RSI(7) with 35/65 thresholds
    r = rsi(closes, 7)
    if r is None:
        return None
    rs = 0.0
    rd = "NONE"
    if r < 25 or r > 75: rs = 1.0
    elif r < 35 or r > 65: rs = 0.7
    elif r < 42 or r > 58: rs = 0.3
    if r < 42: rd = "LONG"
    elif r > 58: rd = "SHORT"

    # MACD(8,17,9) — research says better for 15m
    ml, ms_line, mh = macd(closes, 8, 17, 9)
    mc = 0.0
    md = "NONE"
    if ml is not None and ms_line is not None:
        prev_ml, prev_ms, _ = macd(closes[:-1], 8, 17, 9) if len(closes) > 1 else (None, None, None)
        if prev_ml is not None:
            if (ml > ms_line) != (prev_ml > prev_ms):
                mc = 1.0
            elif mh is not None and _ is not None and abs(mh) > abs(_):
                mc = 0.8
            elif mh is not None and _ is not None and abs(mh) > abs(_) * 0.5:
                mc = 0.5
        md = "LONG" if ml > ms_line else "SHORT"

    # Bollinger Band(20, 2.5) — mean reversion signal
    bb_up, bb_mid, bb_low = bollinger(closes, 20, 2.5)
    bs = 0.0
    bd = "NONE"
    if bb_up is not None:
        price = closes[-1]
        bb_range = bb_up - bb_low
        if bb_range > 0:
            bb_pct = (price - bb_low) / bb_range
            if bb_pct < 0.1 or bb_pct > 0.9:
                bs = 1.0
            elif bb_pct < 0.2 or bb_pct > 0.8:
                bs = 0.7
            elif bb_pct < 0.3 or bb_pct > 0.7:
                bs = 0.4
            bd = "LONG" if bb_pct < 0.3 else "SHORT" if bb_pct > 0.7 else "NONE"

    # Volume
    va = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
    vr = volumes[-1] / va if va > 0 else 0
    vs = 1.0 if vr >= 2.0 else 0.7 if vr >= 1.5 else 0.4 if vr >= 1.2 else 0.0

    # EMA 5/20 crossover (faster for scalping)
    e5 = ema(closes, 5)
    e20 = ema(closes, 20)
    es = 0.0
    ed = "NONE"
    if e5 and e20:
        db = (e5[-1] - e20[-1]) / closes[-1] * 10000
        if len(e5) >= 2 and len(e20) >= 2:
            if (e5[-1] > e20[-1]) != (e5[-2] > e20[-2]):
                es = 1.0
            elif abs(db) > 15:
                es = 0.8
            elif abs(db) > 8:
                es = 0.5
        ed = "LONG" if e5[-1] > e20[-1] else "SHORT"

    total = rs + mc + bs + vs + es
    dirs = [d for d in [rd, md, bd, ed] if d != "NONE"]
    lc = dirs.count("LONG")
    sc = dirs.count("SHORT")
    direction = "LONG" if lc > sc else "SHORT" if sc > lc else "NONE"

    return {"score": round(total, 2), "direction": direction, "rsi": r, "vol_ratio": vr}


# ── Simulator (with partial TP support) ─────────────────────────────
def simulate(candles_by_symbol, config, symbol_list, score_fn=score_v9_original):
    trades = []
    open_positions = []
    pending_orders = []

    score_min = config["score_min"]
    tp_bps = config["tp_bps"]
    sl_bps = config["sl_bps"]
    tp2_bps = config.get("tp2_bps", None)  # partial TP: close 50% at tp_bps, rest at tp2_bps
    max_hold = config.get("max_hold_candles", 999999)
    max_pos = config["max_positions"]
    require_vol = config.get("require_volume", False)
    cancel_after = config.get("cancel_after_candles", 2)
    offset_bps = config.get("offset_bps", 5)
    long_only = config.get("long_only", False)
    short_only = config.get("short_only", False)
    min_hold = config.get("min_hold_candles", 0)

    # Flatten events
    all_events = []
    for sym in symbol_list:
        if sym not in candles_by_symbol:
            continue
        for i, c in enumerate(candles_by_symbol[sym]):
            all_events.append((c["ts"], sym, i))
    all_events.sort(key=lambda x: x[0])

    for ts, sym, idx in all_events:
        candles = candles_by_symbol[sym]

        # Check pending orders
        new_pending = []
        for order in pending_orders:
            if order["symbol"] != sym:
                new_pending.append(order)
                continue
            candle = candles[idx]
            filled = False
            if order["side"] == "LONG" and candle["low"] <= order["limit_price"]:
                filled = True
            elif order["side"] == "SHORT" and candle["high"] >= order["limit_price"]:
                filled = True
            elapsed = idx - order["placed_idx"]
            if elapsed >= cancel_after and not filled:
                continue
            if filled:
                open_positions.append({
                    "symbol": sym, "side": order["side"],
                    "entry_price": order["limit_price"], "entry_idx": idx,
                    "size_usd": config["size_usd"],
                    "partial_closed": False,
                })
            else:
                new_pending.append(order)
        pending_orders = new_pending

        # Check positions for exit
        still_open = []
        for pos in open_positions:
            if pos["symbol"] != sym:
                still_open.append(pos)
                continue

            candle = candles[idx]
            entry = pos["entry_price"]
            hold = idx - pos["entry_idx"]
            size = pos["size_usd"]

            if pos["side"] == "LONG":
                tp_price = entry * (1 + tp_bps / 10000)
                sl_price = entry * (1 - sl_bps / 10000)
                hit_tp = candle["high"] >= tp_price
                hit_sl = candle["low"] <= sl_price and hold >= min_hold
            else:
                tp_price = entry * (1 - tp_bps / 10000)
                sl_price = entry * (1 + sl_bps / 10000)
                hit_tp = candle["low"] <= tp_price
                hit_sl = candle["high"] >= sl_price and hold >= min_hold

            # Partial TP logic
            if tp2_bps and hit_tp and not pos["partial_closed"]:
                # Close 50% at TP1
                half_size = size / 2
                if pos["side"] == "LONG":
                    pnl = (tp_price - entry) / entry * half_size
                else:
                    pnl = (entry - tp_price) / entry * half_size
                trades.append({
                    "symbol": sym, "side": pos["side"],
                    "entry_price": entry, "exit_price": tp_price,
                    "pnl": round(pnl, 4), "exit_reason": "TP1",
                    "hold_candles": hold, "hold_minutes": hold * 15,
                    "size_usd": half_size, "volume": half_size * 2,
                })
                pos["size_usd"] = half_size
                pos["partial_closed"] = True
                # Update TP to TP2, move SL to entry (breakeven)
                if pos["side"] == "LONG":
                    tp_price = entry * (1 + tp2_bps / 10000)
                    sl_price = entry  # breakeven stop
                    hit_tp = candle["high"] >= tp_price
                    hit_sl = False  # don't SL on same candle as TP1
                else:
                    tp_price = entry * (1 - tp2_bps / 10000)
                    sl_price = entry
                    hit_tp = candle["low"] <= tp_price
                    hit_sl = False

            exit_reason = None
            exit_price = None

            if hit_tp and hit_sl:
                exit_reason = "SL"
                exit_price = sl_price
            elif hit_tp:
                exit_reason = "TP2" if pos["partial_closed"] else "TP"
                exit_price = tp_price
            elif hit_sl:
                exit_reason = "SL"
                exit_price = sl_price
            elif hold >= max_hold:
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
                    "pnl": round(pnl, 4), "exit_reason": exit_reason,
                    "hold_candles": hold, "hold_minutes": hold * 15,
                    "size_usd": pos["size_usd"], "volume": pos["size_usd"] * 2,
                })
            else:
                still_open.append(pos)
        open_positions = still_open

        # Score for entry
        signal = score_fn(candles, idx)
        if not signal or signal["score"] < score_min or signal["direction"] == "NONE":
            continue
        if require_vol and signal["vol_ratio"] < 1.2:
            continue
        if long_only and signal["direction"] != "LONG":
            continue
        if short_only and signal["direction"] != "SHORT":
            continue

        current_count = len(open_positions) + len(pending_orders)
        if current_count >= max_pos:
            continue
        if any(p["symbol"] == sym for p in open_positions):
            continue
        if any(o["symbol"] == sym for o in pending_orders):
            continue

        price = candles[idx]["close"]
        if signal["direction"] == "LONG":
            limit = price * (1 - offset_bps / 10000)
        else:
            limit = price * (1 + offset_bps / 10000)

        pending_orders.append({
            "symbol": sym, "side": signal["direction"],
            "limit_price": limit, "placed_idx": idx,
        })

    return trades


# ── Strategies ──────────────────────────────────────────────────────
STRATEGIES = {
    # === ORIGINALS (from v1 run) ===
    "A_fast_scalp_v9": {
        "name": "Fast Scalp (v9 engine)",
        "score_min": 2.5, "tp_bps": 80, "sl_bps": 40,
        "max_hold_candles": 8, "max_positions": 5,
        "size_usd": 100, "offset_bps": 5, "cancel_after_candles": 1,
        "engine": "v9",
    },
    "B_baseline_v9": {
        "name": "Baseline v9",
        "score_min": 3.0, "tp_bps": 150, "sl_bps": 100,
        "max_hold_candles": 96, "max_positions": 5,
        "size_usd": 100, "require_volume": True, "offset_bps": 8,
        "cancel_after_candles": 2, "engine": "v9",
    },
    "F_patient_v9": {
        "name": "Patient Winner (v9)",
        "score_min": 3.0, "tp_bps": 200, "sl_bps": 150,
        "max_hold_candles": 192, "max_positions": 3,
        "size_usd": 100, "require_volume": True, "offset_bps": 8,
        "cancel_after_candles": 4, "engine": "v9",
    },

    # === RESEARCH-BACKED NEW STRATEGIES ===
    "G_research_scalp": {
        "name": "Research Scalp (MACD 8/17/9, RSI 7, BB)",
        "score_min": 2.5, "tp_bps": 80, "sl_bps": 40,
        "max_hold_candles": 8, "max_positions": 5,
        "size_usd": 100, "offset_bps": 5, "cancel_after_candles": 1,
        "engine": "research",
    },
    "H_research_balanced": {
        "name": "Research Balanced (tuned indicators)",
        "score_min": 3.0, "tp_bps": 150, "sl_bps": 100,
        "max_hold_candles": 96, "max_positions": 5,
        "size_usd": 100, "require_volume": True, "offset_bps": 8,
        "cancel_after_candles": 2, "engine": "research",
    },
    "I_partial_tp": {
        "name": "Partial TP (1.5% → 3%, SL to BE)",
        "score_min": 2.5, "tp_bps": 150, "tp2_bps": 300,
        "sl_bps": 100, "max_hold_candles": 48, "max_positions": 5,
        "size_usd": 100, "offset_bps": 5, "cancel_after_candles": 2,
        "engine": "research",
    },
    "J_adaptive_scalper": {
        "name": "Adaptive Scalper (research composite)",
        "score_min": 3.0, "tp_bps": 150, "tp2_bps": 300,
        "sl_bps": 100, "max_hold_candles": 8, "max_positions": 5,
        "size_usd": 100, "require_volume": True,
        "offset_bps": 5, "cancel_after_candles": 1,
        "min_hold_candles": 1,
        "engine": "research",
    },
    "K_high_vol_research": {
        "name": "Volume King (research engine)",
        "score_min": 2.0, "tp_bps": 60, "sl_bps": 40,
        "max_hold_candles": 6, "max_positions": 5,
        "size_usd": 100, "offset_bps": 3, "cancel_after_candles": 1,
        "engine": "research",
    },
    "L_asymmetric_research": {
        "name": "Asymmetric 3:1 (research engine)",
        "score_min": 2.5, "tp_bps": 150, "sl_bps": 50,
        "max_hold_candles": 16, "max_positions": 5,
        "size_usd": 100, "offset_bps": 5, "cancel_after_candles": 2,
        "engine": "research",
    },
    "M_long_only_research": {
        "name": "LONG Only (research engine)",
        "score_min": 2.5, "tp_bps": 100, "sl_bps": 60,
        "max_hold_candles": 16, "max_positions": 5,
        "size_usd": 100, "offset_bps": 5, "cancel_after_candles": 2,
        "long_only": True, "engine": "research",
    },
    "N_short_only_research": {
        "name": "SHORT Only (research engine)",
        "score_min": 2.5, "tp_bps": 100, "sl_bps": 60,
        "max_hold_candles": 16, "max_positions": 5,
        "size_usd": 100, "offset_bps": 5, "cancel_after_candles": 2,
        "short_only": True, "engine": "research",
    },
    "O_patient_research": {
        "name": "Patient Winner (research engine)",
        "score_min": 3.0, "tp_bps": 200, "sl_bps": 150,
        "max_hold_candles": 192, "max_positions": 3,
        "size_usd": 100, "require_volume": True, "offset_bps": 8,
        "cancel_after_candles": 4, "engine": "research",
    },
}


# ── Main ────────────────────────────────────────────────────────────
def main():
    print("Loading cached candle data...")
    with open(CACHE_FILE) as f:
        candles_by_symbol = json.load(f)
    print(f"Loaded {len(candles_by_symbol)} symbols")

    symbols = list(candles_by_symbol.keys())

    # Days covered
    first_ts = min(candles_by_symbol[s][0]["ts"] for s in candles_by_symbol)
    last_ts = max(candles_by_symbol[s][-1]["ts"] for s in candles_by_symbol)
    days = (last_ts - first_ts) / (1000 * 86400)
    print(f"Period: {days:.1f} days\n")

    results = {}

    for sid, config in sorted(STRATEGIES.items()):
        engine = config.get("engine", "v9")
        score_fn = score_research_tuned if engine == "research" else score_v9_original

        print(f"{'─'*60}")
        print(f"{sid}: {config['name']}")
        print(f"  Engine={engine} score>={config['score_min']} TP={config['tp_bps']}bps SL={config['sl_bps']}bps"
              f"{' TP2='+str(config.get('tp2_bps',''))+'bps' if config.get('tp2_bps') else ''}")

        trades = simulate(candles_by_symbol, config, symbols, score_fn)

        if not trades:
            print("  NO TRADES\n")
            results[sid] = {"name": config["name"], "trades": 0}
            continue

        total_pnl = sum(t["pnl"] for t in trades)
        total_vol = sum(t["volume"] for t in trades)
        winners = [t for t in trades if t["pnl"] > 0]
        wr = len(winners) / len(trades) * 100
        daily_trades = len(trades) / days
        daily_vol = total_vol / days
        daily_pnl = total_pnl / days
        avg_win = np.mean([t["pnl"] for t in winners]) if winners else 0
        avg_loss = np.mean([t["pnl"] for t in [t for t in trades if t["pnl"] <= 0]]) if len(trades) > len(winners) else 0
        rr = abs(avg_win / avg_loss) if avg_loss != 0 else 999

        # Max drawdown
        running = 0
        peak = 0
        max_dd = 0
        for t in trades:
            running += t["pnl"]
            peak = max(peak, running)
            max_dd = max(max_dd, peak - running)

        by_reason = defaultdict(lambda: {"count": 0, "pnl": 0})
        for t in trades:
            by_reason[t["exit_reason"]]["count"] += 1
            by_reason[t["exit_reason"]]["pnl"] += t["pnl"]

        print(f"  Trades: {len(trades)} ({daily_trades:.1f}/day) | Vol: ${total_vol:,.0f} (${daily_vol:,.0f}/day)")
        print(f"  PnL: ${total_pnl:+.2f} (${daily_pnl:+.2f}/day) | WR: {wr:.1f}% | R:R: {rr:.2f} | MaxDD: ${max_dd:.2f}")
        for reason, s in sorted(by_reason.items()):
            print(f"    {reason}: {s['count']} trades, ${s['pnl']:+.2f}")
        print()

        results[sid] = {
            "name": config["name"], "engine": engine,
            "trades": len(trades), "daily_trades": round(daily_trades, 1),
            "total_volume": round(total_vol, 2), "daily_volume": round(daily_vol, 2),
            "total_pnl": round(total_pnl, 4), "daily_pnl": round(daily_pnl, 4),
            "win_rate": round(wr, 1), "rr_ratio": round(rr, 2),
            "max_drawdown": round(max_dd, 2),
            "avg_win": round(avg_win, 4), "avg_loss": round(avg_loss, 4),
            "by_reason": {k: dict(v) for k, v in by_reason.items()},
        }

    # Summary
    print("=" * 100)
    print("STRATEGY COMPARISON — Sorted by Daily PnL")
    print("=" * 100)
    print(f"{'Strategy':<42} {'Trades':>6} {'T/day':>6} {'Vol/day':>10} {'PnL':>10} {'PnL/d':>8} {'WR':>6} {'R:R':>5} {'DD':>8}")
    print("-" * 100)

    for sid, r in sorted(results.items(), key=lambda x: x[1].get("daily_pnl", -999), reverse=True):
        if r["trades"] == 0:
            continue
        marker = " ***" if r.get("daily_pnl", 0) > 0 else ""
        print(f"  {r['name']:<40} {r['trades']:>6} {r['daily_trades']:>5.1f} "
              f"${r['daily_volume']:>9,.0f} ${r['total_pnl']:>+9.2f} ${r['daily_pnl']:>+7.2f} "
              f"{r['win_rate']:>5.1f}% {r['rr_ratio']:>5.2f} ${r['max_drawdown']:>7.2f}{marker}")

    print("\n*** = POSITIVE PnL")
    print("=" * 100)

    # Save
    out = RESULTS_DIR / "strategy_backtest_v2_results.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    import os
    os.chdir(Path(__file__).parent.parent)
    main()
