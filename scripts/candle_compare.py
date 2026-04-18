"""
Candle Comparison Test: Extended native vs Binance
Runs every 5 minutes for 1 hour, comparing 15m candles from both sources.
Logs signal differences (RSI, MACD, score, direction) side by side.
"""

import asyncio
import sys
import time
import logging
import requests
import pandas as pd
from datetime import datetime

sys.path.insert(0, "/Users/admin/Documents/Projects/pacifica-trading-bot")

from dexes.extended.extended_sdk import ExtendedSDK
from core.strategies.momentum.engine import MomentumEngine, MomentumConfig
from dotenv import load_dotenv
import os

load_dotenv("/Users/admin/Documents/Projects/pacifica-trading-bot/.env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("/Users/admin/Documents/Projects/pacifica-trading-bot/logs/momentum/candle_compare.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# Assets to compare — pick a mix of what Extended trades
TEST_ASSETS = ["BTC", "ETH", "SOL", "XMR", "LIT", "SUI"]

# Binance symbol mapping
BINANCE_MAP = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XMR": "XMRUSDT",
    "LIT": "LITUSDT",
    "SUI": "SUIUSDT",
}

engine = MomentumEngine(MomentumConfig())


def fetch_binance_candles(asset: str, count: int = 50) -> pd.DataFrame | None:
    """Fetch 15m candles from Binance."""
    symbol = BINANCE_MAP.get(asset)
    if not symbol:
        return None
    try:
        resp = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "15m", "limit": count},
            timeout=10,
        )
        data = resp.json()
        if not data:
            return None
        df = pd.DataFrame(data, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore",
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        logger.error(f"Binance fetch failed for {asset}: {e}")
        return None


async def fetch_extended_candles(sdk: ExtendedSDK, asset: str, count: int = 50) -> pd.DataFrame | None:
    """Fetch 15m candles from Extended native API."""
    market = f"{asset}-USD"
    try:
        candles = await sdk.get_candles(market, interval="15m", limit=count, candle_type="trades")
        if not candles:
            return None
        df = pd.DataFrame(candles)
        # Extended returns o, h, l, c, v, T
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "T": "timestamp"})
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception as e:
        logger.error(f"Extended fetch failed for {asset}: {e}")
        return None


def compare_signals(asset: str, binance_df: pd.DataFrame, extended_df: pd.DataFrame) -> dict:
    """Run engine on both candle sources and compare."""
    b_trend = engine.detect_trend(binance_df)
    e_trend = engine.detect_trend(extended_df)

    # Price comparison (last close)
    b_price = float(binance_df["close"].iloc[-1])
    e_price = float(extended_df["close"].iloc[-1])
    price_diff_bps = abs(b_price - e_price) / b_price * 10000

    # Volume comparison
    b_vol = float(binance_df["volume"].iloc[-1])
    e_vol = float(extended_df["volume"].iloc[-1])

    return {
        "asset": asset,
        "binance_price": round(b_price, 4),
        "extended_price": round(e_price, 4),
        "price_diff_bps": round(price_diff_bps, 2),
        "binance_vol": round(b_vol, 2),
        "extended_vol": round(e_vol, 2),
        "binance_direction": b_trend["direction"],
        "extended_direction": e_trend["direction"],
        "binance_score": b_trend["score"],
        "extended_score": e_trend["score"],
        "binance_rsi": b_trend["rsi"],
        "extended_rsi": e_trend["rsi"],
        "binance_scoring": b_trend["scoring"],
        "extended_scoring": e_trend["scoring"],
        "direction_match": b_trend["direction"] == e_trend["direction"],
        "score_diff": round(abs(b_trend["score"] - e_trend["score"]), 2),
        "rsi_diff": round(abs(b_trend["rsi"] - e_trend["rsi"]), 1),
    }


async def run_cycle(sdk: ExtendedSDK, cycle: int):
    """Run one comparison cycle across all test assets."""
    logger.info(f"{'='*70}")
    logger.info(f"CYCLE {cycle} — {datetime.now().strftime('%H:%M:%S')}")
    logger.info(f"{'='*70}")

    results = []
    mismatches = 0

    for asset in TEST_ASSETS:
        b_df = fetch_binance_candles(asset)
        e_df = await fetch_extended_candles(sdk, asset)

        if b_df is None or len(b_df) < 30:
            logger.warning(f"  {asset}: Binance data insufficient ({len(b_df) if b_df is not None else 0} rows)")
            continue
        if e_df is None or len(e_df) < 30:
            logger.warning(f"  {asset}: Extended data insufficient ({len(e_df) if e_df is not None else 0} rows)")
            continue

        cmp = compare_signals(asset, b_df, e_df)
        results.append(cmp)

        match_str = "MATCH" if cmp["direction_match"] else "MISMATCH"
        if not cmp["direction_match"]:
            mismatches += 1

        logger.info(
            f"  {asset:5s} | price diff: {cmp['price_diff_bps']:6.1f}bps | "
            f"RSI: {cmp['binance_rsi']:5.1f} vs {cmp['extended_rsi']:5.1f} (diff {cmp['rsi_diff']}) | "
            f"score: {cmp['binance_score']} vs {cmp['extended_score']} (diff {cmp['score_diff']}) | "
            f"dir: {cmp['binance_direction']:5s} vs {cmp['extended_direction']:5s} — {match_str}"
        )
        if not cmp["direction_match"]:
            logger.info(f"         Binance:  {cmp['binance_scoring']}")
            logger.info(f"         Extended: {cmp['extended_scoring']}")

    # Volume comparison
    logger.info(f"  --- Volume (raw last candle) ---")
    for r in results:
        logger.info(f"  {r['asset']:5s} | Binance vol: {r['binance_vol']:>14.2f} | Extended vol: {r['extended_vol']:>14.2f}")

    logger.info(f"  SUMMARY: {len(results)} assets compared, {mismatches} direction mismatches")
    return results


async def main():
    logger.info("Candle Compare Test: Extended native vs Binance")
    logger.info(f"Assets: {', '.join(TEST_ASSETS)}")
    logger.info(f"Interval: 15m candles, checking every 5 min for 1 hour")
    logger.info("")

    # Extended SDK — read-only, no signing needed
    sdk = ExtendedSDK(
        api_key=os.getenv("EXTENDED_API_KEY", ""),
    )

    cycles = 12  # 12 x 5min = 1 hour
    all_results = []

    for i in range(1, cycles + 1):
        results = await run_cycle(sdk, i)
        all_results.extend(results)

        if i < cycles:
            logger.info(f"  Next cycle in 5 min...\n")
            await asyncio.sleep(300)

    # Final summary
    logger.info(f"\n{'='*70}")
    logger.info(f"FINAL SUMMARY — {len(all_results)} total comparisons")
    logger.info(f"{'='*70}")

    total_mismatches = sum(1 for r in all_results if not r["direction_match"])
    avg_price_diff = sum(r["price_diff_bps"] for r in all_results) / len(all_results) if all_results else 0
    avg_rsi_diff = sum(r["rsi_diff"] for r in all_results) / len(all_results) if all_results else 0
    avg_score_diff = sum(r["score_diff"] for r in all_results) / len(all_results) if all_results else 0

    logger.info(f"  Direction mismatches: {total_mismatches}/{len(all_results)} ({total_mismatches/len(all_results)*100:.1f}%)")
    logger.info(f"  Avg price diff: {avg_price_diff:.1f} bps")
    logger.info(f"  Avg RSI diff:   {avg_rsi_diff:.1f}")
    logger.info(f"  Avg score diff: {avg_score_diff:.2f}")

    # Per-asset breakdown
    for asset in TEST_ASSETS:
        asset_results = [r for r in all_results if r["asset"] == asset]
        if asset_results:
            mm = sum(1 for r in asset_results if not r["direction_match"])
            ap = sum(r["price_diff_bps"] for r in asset_results) / len(asset_results)
            ar = sum(r["rsi_diff"] for r in asset_results) / len(asset_results)
            logger.info(f"  {asset:5s}: {mm}/{len(asset_results)} mismatches, avg price diff {ap:.1f}bps, avg RSI diff {ar:.1f}")

    logger.info(f"\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
