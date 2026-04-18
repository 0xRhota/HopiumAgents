#!/usr/bin/env python3
"""
Trade Pattern Analysis — What separates winners from losers?
=============================================================
Pure statistical analysis of our 616 historical trades.
No external API calls. No LLM calls. Just math.

Run: python3 scripts/mcp_backtest.py
Output: research/mcp_backtest_results/
"""

import json
import glob
import numpy as np
from pathlib import Path
from collections import defaultdict

LOG_DIR = Path("logs/momentum")
RESULTS_DIR = Path("research/mcp_backtest_results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def load_all_trades():
    trades = []
    seen_ids = set()
    for pattern in [str(LOG_DIR / "nado_*_trades.jsonl"), str(LOG_DIR / "hibachi_*_trades.jsonl"),
                    str(LOG_DIR / "nado_trades.jsonl"), str(LOG_DIR / "hibachi_trades.jsonl")]:
        for f in glob.glob(pattern):
            with open(f) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                        tid = t.get("id", "")
                        if tid and tid in seen_ids:
                            continue
                        seen_ids.add(tid)
                        t["pnl"] = t.get("pnl", t.get("pnl_delta", 0))
                        t["symbol"] = t.get("symbol", t.get("asset", "?"))
                        t["score"] = t.get("score", t.get("trend_strength", 0))
                        if t["score"] and t["score"] <= 1.0:
                            t["score"] = t["score"] * 5.0  # normalize old 0-1 format to 0-5
                        trades.append(t)
                    except json.JSONDecodeError:
                        continue
    print(f"Loaded {len(trades)} unique trades")
    return trades


def analyze(trades):
    report = []
    p = report.append

    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    wr = len(winners) / len(trades) * 100 if trades else 0

    p("=" * 72)
    p("TRADE PATTERN ANALYSIS — What Would Have Made Us Better?")
    p("=" * 72)
    p(f"Total: {len(trades)} trades | Winners: {len(winners)} | Losers: {len(losers)}")
    p(f"Win Rate: {wr:.1f}% | Total PnL: ${total_pnl:+.2f}")
    p(f"Avg Win: ${np.mean([t['pnl'] for t in winners]):+.4f}" if winners else "")
    p(f"Avg Loss: ${np.mean([t['pnl'] for t in losers]):+.4f}" if losers else "")
    p("")

    # ── BY EXCHANGE ─────────────────────────────────────────────────
    p("── BY EXCHANGE ──")
    for ex in sorted(set(t.get("exchange", "?") for t in trades)):
        ex_t = [t for t in trades if t.get("exchange") == ex]
        ex_w = [t for t in ex_t if t["pnl"] > 0]
        ex_pnl = sum(t["pnl"] for t in ex_t)
        ex_wr = len(ex_w) / len(ex_t) * 100 if ex_t else 0
        p(f"  {ex:10s}: {len(ex_t):>4} trades | WR {ex_wr:5.1f}% | PnL ${ex_pnl:>+10.2f}")
    p("")

    # ── BY SIDE ─────────────────────────────────────────────────────
    p("── BY SIDE ──")
    for side in ["LONG", "SHORT"]:
        s_t = [t for t in trades if t.get("side") == side]
        if not s_t:
            continue
        s_w = [t for t in s_t if t["pnl"] > 0]
        s_pnl = sum(t["pnl"] for t in s_t)
        s_wr = len(s_w) / len(s_t) * 100
        p(f"  {side:6s}: {len(s_t):>4} trades | WR {s_wr:5.1f}% | PnL ${s_pnl:>+10.2f}")

    # By exchange + side
    for ex in ["nado", "hibachi"]:
        for side in ["LONG", "SHORT"]:
            es_t = [t for t in trades if t.get("exchange") == ex and t.get("side") == side]
            if not es_t:
                continue
            es_w = [t for t in es_t if t["pnl"] > 0]
            es_pnl = sum(t["pnl"] for t in es_t)
            es_wr = len(es_w) / len(es_t) * 100
            p(f"    {ex}/{side}: {len(es_t):>3} trades | WR {es_wr:5.1f}% | PnL ${es_pnl:>+8.2f}")
    p("")

    # ── BY EXIT REASON ──────────────────────────────────────────────
    p("── BY EXIT REASON ──")
    for reason in sorted(set(t.get("exit_reason", "?") for t in trades)):
        r_t = [t for t in trades if t.get("exit_reason") == reason]
        r_w = [t for t in r_t if t["pnl"] > 0]
        r_pnl = sum(t["pnl"] for t in r_t)
        r_wr = len(r_w) / len(r_t) * 100 if r_t else 0
        avg_hold = np.mean([t.get("hold_minutes", 0) for t in r_t])
        p(f"  {reason:15s}: {len(r_t):>4} trades | WR {r_wr:5.1f}% | PnL ${r_pnl:>+10.2f} | Avg hold {avg_hold:.0f}min")
    p("")

    # ── BY SYMBOL ───────────────────────────────────────────────────
    p("── BY SYMBOL (sorted by PnL) ──")
    sym_stats = {}
    for sym in sorted(set(t["symbol"] for t in trades)):
        s_t = [t for t in trades if t["symbol"] == sym]
        s_pnl = sum(t["pnl"] for t in s_t)
        s_wr = len([t for t in s_t if t["pnl"] > 0]) / len(s_t) * 100 if s_t else 0
        sym_stats[sym] = {"trades": len(s_t), "pnl": s_pnl, "wr": s_wr}

    for sym, stats in sorted(sym_stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
        flag = "🔥" if stats["pnl"] > 0 else "💀"
        p(f"  {sym:8s}: {stats['trades']:>3} trades | WR {stats['wr']:5.1f}% | PnL ${stats['pnl']:>+10.2f} {flag}")
    p("")

    # ── SCORE ANALYSIS ──────────────────────────────────────────────
    p("── BY V9 SCORE BUCKET ──")
    scored = [t for t in trades if t["score"] and t["score"] > 0]
    if scored:
        buckets = [(2.0, 2.5), (2.5, 3.0), (3.0, 3.5), (3.5, 4.0), (4.0, 4.5), (4.5, 5.0)]
        for lo, hi in buckets:
            b_t = [t for t in scored if lo <= t["score"] < hi]
            if not b_t:
                continue
            b_pnl = sum(t["pnl"] for t in b_t)
            b_wr = len([t for t in b_t if t["pnl"] > 0]) / len(b_t) * 100
            p(f"  {lo:.1f}-{hi:.1f}: {len(b_t):>4} trades | WR {b_wr:5.1f}% | PnL ${b_pnl:>+10.2f}")
    p("")

    # ── HOLD TIME ANALYSIS ──────────────────────────────────────────
    p("── BY HOLD TIME ──")
    timed = [t for t in trades if t.get("hold_minutes", 0) > 0]
    if timed:
        bins = [(0, 5), (5, 15), (15, 60), (60, 240), (240, 1440), (1440, 99999)]
        labels = ["0-5min", "5-15min", "15-60min", "1-4hr", "4-24hr", "24hr+"]
        for (lo, hi), label in zip(bins, labels):
            b_t = [t for t in timed if lo <= t.get("hold_minutes", 0) < hi]
            if not b_t:
                continue
            b_pnl = sum(t["pnl"] for t in b_t)
            b_wr = len([t for t in b_t if t["pnl"] > 0]) / len(b_t) * 100
            p(f"  {label:10s}: {len(b_t):>4} trades | WR {b_wr:5.1f}% | PnL ${b_pnl:>+10.2f}")
    p("")

    # ── TREND STRENGTH ANALYSIS ─────────────────────────────────────
    p("── BY TREND STRENGTH (ROC) ──")
    roc_trades = [t for t in trades if t.get("roc_bps", 0) > 0]
    if roc_trades:
        bins = [(0, 5), (5, 10), (10, 20), (20, 50), (50, 999)]
        labels = ["0-5bps", "5-10bps", "10-20bps", "20-50bps", "50+bps"]
        for (lo, hi), label in zip(bins, labels):
            b_t = [t for t in roc_trades if lo <= t.get("roc_bps", 0) < hi]
            if not b_t:
                continue
            b_pnl = sum(t["pnl"] for t in b_t)
            b_wr = len([t for t in b_t if t["pnl"] > 0]) / len(b_t) * 100
            p(f"  {label:10s}: {len(b_t):>4} trades | WR {b_wr:5.1f}% | PnL ${b_pnl:>+10.2f}")
    p("")

    # ── SIMULATED FILTERS ───────────────────────────────────────────
    p("── SIMULATED FILTERS (what if we had filtered?) ──")
    p(f"{'Filter':<40} {'Trades':>7} {'WR':>7} {'PnL':>12} {'vs Base':>10}")
    p("-" * 78)

    baseline_pnl = total_pnl
    baseline_trades = len(trades)

    filters = {}

    # Filter: Only TREND_FLIP exits (remove SL, TP, TIME, EMERGENCY_SL)
    tf_only = [t for t in trades if t.get("exit_reason") in ("TREND_FLIP",)]
    if tf_only:
        f_pnl = sum(t["pnl"] for t in tf_only)
        f_wr = len([t for t in tf_only if t["pnl"] > 0]) / len(tf_only) * 100
        filters["only_trend_flip_exits"] = (len(tf_only), f_wr, f_pnl)

    # Filter: Score >= 3.5 (stricter than current 3.0)
    high_score = [t for t in scored if t["score"] >= 3.5]
    if high_score:
        f_pnl = sum(t["pnl"] for t in high_score)
        f_wr = len([t for t in high_score if t["pnl"] > 0]) / len(high_score) * 100
        filters["score_min_3.5"] = (len(high_score), f_wr, f_pnl)

    # Filter: Score >= 4.0
    very_high = [t for t in scored if t["score"] >= 4.0]
    if very_high:
        f_pnl = sum(t["pnl"] for t in very_high)
        f_wr = len([t for t in very_high if t["pnl"] > 0]) / len(very_high) * 100
        filters["score_min_4.0"] = (len(very_high), f_wr, f_pnl)

    # Filter: Remove worst symbols
    worst_syms = [sym for sym, s in sym_stats.items() if s["pnl"] < -2.0 and s["trades"] >= 5]
    if worst_syms:
        no_worst = [t for t in trades if t["symbol"] not in worst_syms]
        f_pnl = sum(t["pnl"] for t in no_worst)
        f_wr = len([t for t in no_worst if t["pnl"] > 0]) / len(no_worst) * 100
        filters[f"remove_worst_syms({','.join(worst_syms[:5])})"] = (len(no_worst), f_wr, f_pnl)

    # Filter: Only SHORT on nado (historically SHORT > LONG there per CLAUDE.md)
    nado_short = [t for t in trades if t.get("exchange") == "nado" and t.get("side") == "SHORT"]
    hib_all = [t for t in trades if t.get("exchange") == "hibachi"]
    nado_short_hib_all = nado_short + hib_all
    if nado_short_hib_all:
        f_pnl = sum(t["pnl"] for t in nado_short_hib_all)
        f_wr = len([t for t in nado_short_hib_all if t["pnl"] > 0]) / len(nado_short_hib_all) * 100
        filters["nado_short_only+hibachi_all"] = (len(nado_short_hib_all), f_wr, f_pnl)

    # Filter: Hold > 15min only (skip quick SL hits)
    long_hold = [t for t in timed if t.get("hold_minutes", 0) >= 15]
    if long_hold:
        f_pnl = sum(t["pnl"] for t in long_hold)
        f_wr = len([t for t in long_hold if t["pnl"] > 0]) / len(long_hold) * 100
        filters["hold_min_15min"] = (len(long_hold), f_wr, f_pnl)

    # Filter: ROC > 10bps (only trade when momentum is strong)
    strong_roc = [t for t in roc_trades if t.get("roc_bps", 0) >= 10]
    if strong_roc:
        f_pnl = sum(t["pnl"] for t in strong_roc)
        f_wr = len([t for t in strong_roc if t["pnl"] > 0]) / len(strong_roc) * 100
        filters["roc_min_10bps"] = (len(strong_roc), f_wr, f_pnl)

    # Filter: Only top 5 symbols by WR (min 10 trades)
    top_syms = [sym for sym, s in sym_stats.items() if s["wr"] >= 45 and s["trades"] >= 10]
    if top_syms:
        top_only = [t for t in trades if t["symbol"] in top_syms]
        f_pnl = sum(t["pnl"] for t in top_only)
        f_wr = len([t for t in top_only if t["pnl"] > 0]) / len(top_only) * 100
        filters[f"top_syms_only({','.join(top_syms[:5])})"] = (len(top_only), f_wr, f_pnl)

    # Filter: score >= 3.5 AND hold >= 15min
    combo1 = [t for t in scored if t["score"] >= 3.5 and t.get("hold_minutes", 0) >= 15]
    if combo1:
        f_pnl = sum(t["pnl"] for t in combo1)
        f_wr = len([t for t in combo1 if t["pnl"] > 0]) / len(combo1) * 100
        filters["score>=3.5_AND_hold>=15min"] = (len(combo1), f_wr, f_pnl)

    for name, (count, wr, pnl) in sorted(filters.items(), key=lambda x: x[1][2], reverse=True):
        diff = pnl - baseline_pnl
        p(f"  {name:<38} {count:>7} {wr:>6.1f}% ${pnl:>+10.2f} ${diff:>+8.2f}")

    p("")
    p("── KEY TAKEAWAYS ──")

    # Auto-generate takeaways
    best_filter = max(filters.items(), key=lambda x: x[1][2]) if filters else None
    if best_filter:
        p(f"  Best filter: {best_filter[0]}")
        p(f"    → {best_filter[1][0]} trades, {best_filter[1][1]:.1f}% WR, ${best_filter[1][2]:+.2f} PnL")
        p(f"    → ${best_filter[1][2] - baseline_pnl:+.2f} improvement over baseline")

    # Best exchange
    best_ex = max([(ex, sum(t["pnl"] for t in [t for t in trades if t.get("exchange") == ex]))
                    for ex in set(t.get("exchange", "?") for t in trades)], key=lambda x: x[1])
    p(f"  Best exchange: {best_ex[0]} (${best_ex[1]:+.2f})")

    # Best side
    for side in ["LONG", "SHORT"]:
        s_t = [t for t in trades if t.get("side") == side]
        if s_t:
            s_pnl = sum(t["pnl"] for t in s_t)
            s_wr = len([t for t in s_t if t["pnl"] > 0]) / len(s_t) * 100
            p(f"  {side}: WR={s_wr:.1f}%, PnL=${s_pnl:+.2f}")

    p("")
    p("=" * 72)

    # Print and save
    report_text = "\n".join(report)
    print(report_text)

    with open(RESULTS_DIR / "backtest_report.txt", "w") as f:
        f.write(report_text)

    # Save structured data
    with open(RESULTS_DIR / "filter_stats.json", "w") as f:
        json.dump({k: {"trades": v[0], "wr": v[1], "pnl": v[2]} for k, v in filters.items()}, f, indent=2)

    print(f"\nSaved to {RESULTS_DIR}/")


if __name__ == "__main__":
    import os
    os.chdir(Path(__file__).parent.parent)
    analyze(load_all_trades())
