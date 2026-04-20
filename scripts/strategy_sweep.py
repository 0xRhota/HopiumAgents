"""Sweep strategy configs across all tradeable symbols on an exchange.

Tests the hypothesis: does the Paradex-style "slow" config (high score
threshold, wide ATR exits, fewer trades) beat the current "fast" config
(score≥2.5, 80/40 fixed) when applied across every symbol the exchange
offers — including newer/lower-liquidity ones?

Usage:
    python3 scripts/strategy_sweep.py --exchange nado --days 30
    python3 scripts/strategy_sweep.py --exchange nado --days 30 --preset slow --only-winners

Output:
    logs/sweeps/{exchange}_sweep_{ts}.csv
    Printed ranked table to stdout
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from core.backtest.exchange_sim import NADO, HIBACHI, PARADEX
from core.backtest.momentum_strategy import BacktestMomentumStrategy
from core.backtest.runner import run_backtest
from scripts.run_backtest import fetch_binance_klines


_EX_MAP = {"nado": NADO, "hibachi": HIBACHI, "paradex": PARADEX}


async def _list_nado_symbols():
    """Live list of Nado tradeable perps."""
    from dexes.nado.nado_sdk import NadoSDK
    import os
    sdk = NadoSDK(
        wallet_address=os.getenv("NADO_WALLET_ADDRESS"),
        linked_signer_private_key=os.getenv("NADO_LINKED_SIGNER_PRIVATE_KEY"),
    )
    sym_map = await sdk.fetch_symbols_map()  # {product_id: "LIT-PERP", ...}
    return sorted(set(sym_map.values()))


async def _list_symbols(exchange: str):
    if exchange == "nado":
        return await _list_nado_symbols()
    raise NotImplementedError(f"Symbol listing for {exchange} not wired yet — add here.")


def _run_one(symbol: str, preset: str, exchange_spec, days: int):
    """Returns dict with metrics, or None if insufficient data."""
    try:
        bars = fetch_binance_klines(symbol, days)
    except Exception:
        return None
    if len(bars) < 100:
        return None  # Binance returned no data or too little

    strategy = BacktestMomentumStrategy(symbol=symbol, preset=preset,
                                         exchange=exchange_spec.name)
    fills = run_backtest(
        strategy=strategy, bars=bars, exchange=exchange_spec,
        starting_equity=100.0, leverage=10.0,
    )
    realized = sum(f.realized_pnl_usd or 0 for f in fills)
    fees = sum(f.fee for f in fills)
    net = realized - fees
    closes = [f for f in fills if f.opens_or_closes == "CLOSE"]
    wins = sum(1 for f in closes if (f.realized_pnl_usd or 0) > 0)
    wr = wins / len(closes) * 100 if closes else 0
    makers = sum(1 for f in fills if f.is_maker)
    maker_pct = makers / max(1, len(fills)) * 100
    return {
        "symbol": symbol, "preset": preset,
        "bars": len(bars),
        "fills": len(fills),
        "trades": len(closes),
        "wr_pct": round(wr, 1),
        "maker_pct": round(maker_pct, 1),
        "realized": round(realized, 3),
        "fees": round(fees, 3),
        "net": round(net, 3),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", required=True, choices=list(_EX_MAP))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--presets", default="fast,slow")
    ap.add_argument("--only-winners", action="store_true",
                    help="Print only symbols where NET > 0")
    ap.add_argument("--symbols", default=None,
                    help="Comma-separated explicit symbol list (else auto-discover)")
    args = ap.parse_args()

    spec = _EX_MAP[args.exchange]
    presets = [p.strip() for p in args.presets.split(",")]

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        print(f"Discovering symbols on {args.exchange}...")
        symbols = await _list_symbols(args.exchange)
        print(f"Found {len(symbols)} symbols to test.")

    results = []
    for sym in symbols:
        for preset in presets:
            r = _run_one(sym, preset, spec, args.days)
            if r is None:
                continue
            results.append(r)
            marker = "✓" if r["net"] > 0 else "✗"
            print(f"  {marker} {sym:15s} [{preset}] "
                  f"trades={r['trades']:3d} wr={r['wr_pct']:5.1f}% "
                  f"mkr={r['maker_pct']:5.1f}% net=${r['net']:+7.2f}")

    if not results:
        print("No results. Check Binance kline availability.")
        return

    # Save CSV
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_dir = Path("logs/sweeps")
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{args.exchange}_sweep_{ts}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"\nSaved {len(results)} rows → {csv_path}")

    # Ranked summaries per preset
    for preset in presets:
        rows = [r for r in results if r["preset"] == preset]
        if args.only_winners:
            rows = [r for r in rows if r["net"] > 0]
        rows.sort(key=lambda r: r["net"], reverse=True)
        total = sum(r["net"] for r in rows)
        won = sum(1 for r in rows if r["net"] > 0)
        print(f"\n=== {preset.upper()} — top 15 ===")
        print(f"{'symbol':<15} {'trades':>6} {'wr%':>6} {'mkr%':>6} {'net':>10}")
        for r in rows[:15]:
            print(f"{r['symbol']:<15} {r['trades']:>6d} {r['wr_pct']:>5.1f}% "
                  f"{r['maker_pct']:>5.1f}% ${r['net']:>+8.2f}")
        print(f"  Total symbols tested: {len(rows)}  profitable: {won}  "
              f"aggregate NET: ${total:+.2f}")


if __name__ == "__main__":
    asyncio.run(main())
