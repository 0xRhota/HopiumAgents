#!/usr/bin/env python3
"""
Self-Learning Backtest: Replay real trades through self-learning filters.

Loads all JSONL trade records from logs/momentum/, replays them chronologically,
and compares baseline P&L vs self-learning filtered P&L.

Filters applied:
1. Symbol Block — <30% WR after 10+ trades in rolling 7-day window
2. Circuit Breaker — 5 consecutive losses on an exchange → 1h pause
3. Score Calibration — WR < 35% in a score bucket after 8+ trades → block bucket

All data is real exchange trade records. No simulation.
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs" / "momentum"


def load_all_trades() -> list[dict]:
    """Load all JSONL trade records, sorted by timestamp."""
    trades = []
    seen_ids = set()

    for f in LOG_DIR.glob("*_trades.jsonl"):
        # Skip aggregate files (e.g. hibachi_trades.jsonl) — use per-asset files
        name = f.stem  # e.g. "hibachi_sol_trades"
        parts = name.replace("_trades", "").split("_")
        if len(parts) < 2:
            # This is an aggregate file like "hibachi_trades" — skip to avoid double-counting
            continue

        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    trade = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Deduplicate by trade id
                tid = trade.get("id", "")
                if tid and tid in seen_ids:
                    continue
                seen_ids.add(tid)

                # Must have timestamp and pnl
                ts = trade.get("_timestamp")
                if not ts:
                    continue
                if "pnl" not in trade:
                    continue

                trade["_parsed_time"] = datetime.fromisoformat(ts)
                trades.append(trade)

    trades.sort(key=lambda t: t["_parsed_time"])
    return trades


class SymbolTracker:
    """Rolling 7-day symbol win rate tracker."""

    def __init__(self, lookback_days: int = 7, min_trades: int = 10, block_wr: float = 0.30):
        self.lookback_days = lookback_days
        self.min_trades = min_trades
        self.block_wr = block_wr
        self.trades: list[dict] = []  # [{symbol, pnl, time}]

    def record(self, symbol: str, pnl: float, ts: datetime):
        self.trades.append({"symbol": symbol, "pnl": pnl, "time": ts})

    def is_blocked(self, symbol: str, now: datetime) -> tuple[bool, str]:
        cutoff = now - timedelta(days=self.lookback_days)
        recent = [t for t in self.trades if t["symbol"] == symbol and t["time"] >= cutoff]
        total = len(recent)
        if total < self.min_trades:
            return False, ""
        wins = sum(1 for t in recent if t["pnl"] > 0)
        wr = wins / total
        if wr < self.block_wr:
            return True, f"SYMBOL_BLOCKED: {symbol} WR={wr:.0%} ({total} trades)"
        return False, ""


class SimpleCircuitBreaker:
    """Consecutive loss tracker with time-based cooldown."""

    def __init__(self, max_consecutive: int = 5, cooldown_minutes: int = 60):
        self.max_consecutive = max_consecutive
        self.cooldown_minutes = cooldown_minutes
        self.consecutive_losses = 0
        self.cooldown_until: datetime | None = None

    def record(self, pnl: float, ts: datetime):
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.max_consecutive and not self.cooldown_until:
                self.cooldown_until = ts + timedelta(minutes=self.cooldown_minutes)
        else:
            self.consecutive_losses = 0
            # Win also clears cooldown
            self.cooldown_until = None

    def is_triggered(self, now: datetime) -> tuple[bool, str]:
        if self.cooldown_until and now < self.cooldown_until:
            remaining = (self.cooldown_until - now).total_seconds() / 60
            return True, f"CIRCUIT_BREAKER: {self.consecutive_losses} losses ({remaining:.0f}min left)"
        if self.cooldown_until and now >= self.cooldown_until:
            # Cooldown expired
            self.cooldown_until = None
            self.consecutive_losses = 0
        return False, ""


class ScoreBucketTracker:
    """Track WR per score bucket."""

    BUCKETS = [(3.0, 3.5), (3.5, 4.0), (4.0, 4.5), (4.5, 5.01)]

    def __init__(self, min_trades: int = 8, block_wr: float = 0.35):
        self.min_trades = min_trades
        self.block_wr = block_wr
        self.stats: dict[str, dict] = {}

    def _bucket_name(self, score: float) -> str:
        for lo, hi in self.BUCKETS:
            if lo <= score < hi:
                return f"{lo:.1f}-{hi:.1f}"
        return "unknown"

    def record(self, score: float, pnl: float):
        if score is None or score < 3.0:
            return
        bucket = self._bucket_name(score)
        if bucket not in self.stats:
            self.stats[bucket] = {"wins": 0, "losses": 0, "total": 0}
        self.stats[bucket]["total"] += 1
        if pnl > 0:
            self.stats[bucket]["wins"] += 1
        else:
            self.stats[bucket]["losses"] += 1

    def is_blocked(self, score: float) -> tuple[bool, str]:
        if score is None or score < 3.0:
            return False, ""
        bucket = self._bucket_name(score)
        s = self.stats.get(bucket)
        if not s or s["total"] < self.min_trades:
            return False, ""
        wr = s["wins"] / s["total"]
        if wr < self.block_wr:
            return True, f"SCORE_BUCKET_LOW: {bucket} WR={wr:.0%} ({s['total']} trades)"
        return False, ""


def run_backtest(enable_symbol_block: bool = True, label: str = "ALL FILTERS"):
    trades = load_all_trades()
    print(f"Loaded {len(trades)} real trades from {LOG_DIR}")
    if not trades:
        print("No trades found!")
        return

    # Date range
    first = trades[0]["_parsed_time"]
    last = trades[-1]["_parsed_time"]
    print(f"Date range: {first.strftime('%Y-%m-%d %H:%M')} → {last.strftime('%Y-%m-%d %H:%M')}")
    print()

    # Per-exchange state
    exchanges = set(t.get("exchange", "unknown") for t in trades)
    symbol_trackers = {ex: SymbolTracker() for ex in exchanges}
    circuit_breakers = {ex: SimpleCircuitBreaker() for ex in exchanges}
    score_trackers = {ex: ScoreBucketTracker() for ex in exchanges}

    # Results
    baseline = {"trades": 0, "wins": 0, "pnl": 0.0}
    filtered = {"trades": 0, "wins": 0, "pnl": 0.0}
    blocked_by = {"symbol": {"count": 0, "pnl_avoided": 0.0},
                  "circuit": {"count": 0, "pnl_avoided": 0.0},
                  "score": {"count": 0, "pnl_avoided": 0.0}}

    per_exchange_baseline = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    per_exchange_filtered = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})

    blocked_symbol_detail = defaultdict(lambda: {"blocked": 0, "pnl_avoided": 0.0})

    for trade in trades:
        exchange = trade.get("exchange", "unknown")
        symbol = trade.get("symbol", "?")
        pnl = trade.get("pnl", 0.0)
        score = trade.get("score")
        ts = trade["_parsed_time"]
        is_win = pnl > 0

        # Baseline — all trades count
        baseline["trades"] += 1
        baseline["pnl"] += pnl
        if is_win:
            baseline["wins"] += 1
        per_exchange_baseline[exchange]["trades"] += 1
        per_exchange_baseline[exchange]["pnl"] += pnl
        if is_win:
            per_exchange_baseline[exchange]["wins"] += 1

        # Check filters BEFORE recording this trade
        blocked = False
        block_reason = ""

        # 1. Circuit breaker
        tripped, reason = circuit_breakers[exchange].is_triggered(ts)
        if tripped:
            blocked = True
            block_reason = reason
            blocked_by["circuit"]["count"] += 1
            blocked_by["circuit"]["pnl_avoided"] += pnl

        # 2. Symbol block (only if not already blocked and enabled)
        if not blocked and enable_symbol_block:
            sym_blocked, reason = symbol_trackers[exchange].is_blocked(symbol, ts)
            if sym_blocked:
                blocked = True
                block_reason = reason
                blocked_by["symbol"]["count"] += 1
                blocked_by["symbol"]["pnl_avoided"] += pnl
                blocked_symbol_detail[f"{exchange}:{symbol}"]["blocked"] += 1
                blocked_symbol_detail[f"{exchange}:{symbol}"]["pnl_avoided"] += pnl

        # 3. Score bucket (only if not already blocked)
        if not blocked and score is not None:
            sc_blocked, reason = score_trackers[exchange].is_blocked(score)
            if sc_blocked:
                blocked = True
                block_reason = reason
                blocked_by["score"]["count"] += 1
                blocked_by["score"]["pnl_avoided"] += pnl

        # Record in filtered results
        if not blocked:
            filtered["trades"] += 1
            filtered["pnl"] += pnl
            if is_win:
                filtered["wins"] += 1
            per_exchange_filtered[exchange]["trades"] += 1
            per_exchange_filtered[exchange]["pnl"] += pnl
            if is_win:
                per_exchange_filtered[exchange]["wins"] += 1

        # ALWAYS record trade result for future filter decisions
        # (even blocked trades contribute to learning — they still happened in reality)
        symbol_trackers[exchange].record(symbol, pnl, ts)
        circuit_breakers[exchange].record(pnl, ts)
        if score is not None:
            score_trackers[exchange].record(score, pnl)

    # === OUTPUT ===
    print("=" * 70)
    print(f"SELF-LEARNING BACKTEST — {label} ({baseline['trades']} real trades)")
    print("=" * 70)
    print()

    b_wr = baseline["wins"] / baseline["trades"] * 100 if baseline["trades"] else 0
    print(f"BASELINE (no filters):")
    print(f"  Total trades: {baseline['trades']} | Wins: {baseline['wins']} | "
          f"WR: {b_wr:.1f}% | Total PnL: ${baseline['pnl']:+.4f}")
    print()

    f_wr = filtered["wins"] / filtered["trades"] * 100 if filtered["trades"] else 0
    total_blocked = baseline["trades"] - filtered["trades"]
    print(f"WITH SELF-LEARNING:")
    print(f"  Trades taken: {filtered['trades']} ({total_blocked} blocked) | "
          f"Wins: {filtered['wins']} | WR: {f_wr:.1f}% | Total PnL: ${filtered['pnl']:+.4f}")
    print()

    pnl_delta = filtered["pnl"] - baseline["pnl"]
    print(f"DELTA: ${pnl_delta:+.4f} (self-learning {'saves' if pnl_delta > 0 else 'costs'} "
          f"${abs(pnl_delta):.4f})")
    print()

    print(f"BLOCKED TRADE BREAKDOWN:")
    for name, stats in blocked_by.items():
        label = {"symbol": "Symbol blocked", "circuit": "Circuit breaker", "score": "Score bucket"}[name]
        print(f"  {label}: {stats['count']} trades (${stats['pnl_avoided']:+.4f} avoided PnL)")
    print()

    print(f"PER-EXCHANGE:")
    for ex in sorted(exchanges):
        b = per_exchange_baseline[ex]
        f = per_exchange_filtered[ex]
        b_wr_ex = b["wins"] / b["trades"] * 100 if b["trades"] else 0
        f_wr_ex = f["wins"] / f["trades"] * 100 if f["trades"] else 0
        delta = f["pnl"] - b["pnl"]
        print(f"  {ex:10s}: {b['trades']:3d} trades ${b['pnl']:+8.4f} ({b_wr_ex:.0f}% WR) "
              f"→ {f['trades']:3d} trades ${f['pnl']:+8.4f} ({f_wr_ex:.0f}% WR) "
              f"[delta ${delta:+.4f}]")
    print()

    # Blocked symbols detail
    if blocked_symbol_detail:
        print(f"SYMBOL BLOCKS (would be blocked after enough losing trades):")
        for key, stats in sorted(blocked_symbol_detail.items(), key=lambda x: x[1]["pnl_avoided"]):
            print(f"  {key}: {stats['blocked']} trades blocked (${stats['pnl_avoided']:+.4f} avoided)")
        print()

    # Score bucket stats
    print(f"SCORE BUCKET STATS (final state):")
    for ex in sorted(exchanges):
        st = score_trackers[ex]
        if st.stats:
            print(f"  {ex}:")
            for bucket, s in sorted(st.stats.items()):
                wr = s["wins"] / s["total"] * 100 if s["total"] else 0
                flag = " << WOULD BLOCK" if s["total"] >= 8 and wr < 35 else ""
                print(f"    {bucket}: {s['wins']}/{s['total']} ({wr:.0f}% WR){flag}")
    print()

    # Circuit breaker summary
    print(f"CIRCUIT BREAKER SUMMARY:")
    for ex in sorted(exchanges):
        cb = circuit_breakers[ex]
        print(f"  {ex}: final consecutive_losses={cb.consecutive_losses}")

    print()
    print("=" * 70)
    print("All data sourced from real exchange JSONL trade records.")
    print(f"Source: {LOG_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("RUN 1: ALL FILTERS (symbol block + circuit breaker + score bucket)")
    print("=" * 70 + "\n")
    run_backtest(enable_symbol_block=True, label="ALL FILTERS")

    print("\n\n")
    print("=" * 70)
    print("RUN 2: WITHOUT SYMBOL BLOCK (circuit breaker + score bucket only)")
    print("=" * 70 + "\n")
    run_backtest(enable_symbol_block=False, label="NO SYMBOL BLOCK")
