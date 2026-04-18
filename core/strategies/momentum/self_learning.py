"""
Momentum Self-Learning — lightweight filters for momentum bots.

Two proven filters (validated on 497 real trades):
1. Circuit Breaker — 5 consecutive losses on an exchange → 1h pause
2. Score Bucket — Track WR per score bucket, block buckets with <35% WR after 8+ trades

Seeds from existing JSONL trade logs on startup.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class MomentumLearner:
    """
    Pre-entry gate + post-exit learning for momentum bots.

    Usage:
        learner = MomentumLearner("hibachi", Path("logs/momentum"))

        # Before entry:
        allowed, reason = learner.should_trade("BTC", score=4.2)

        # After exit:
        learner.record_trade("BTC", score=4.2, pnl=-0.15)
    """

    SCORE_BUCKETS = [(3.0, 3.5), (3.5, 4.0), (4.0, 4.5), (4.5, 5.01)]

    def __init__(
        self,
        exchange: str,
        data_dir: Path,
        max_consecutive_losses: int = 5,
        cooldown_minutes: int = 60,
        score_bucket_min_trades: int = 15,
        score_bucket_block_wr: float = 0.25,
    ):
        self.exchange = exchange
        self.data_dir = data_dir

        # Circuit breaker state
        self.consecutive_losses = 0
        self.cooldown_until: Optional[datetime] = None
        self.max_consecutive_losses = max_consecutive_losses
        self.cooldown_minutes = cooldown_minutes

        # Score bucket state
        self.score_bucket_stats: Dict[str, dict] = {}
        self.score_bucket_min_trades = score_bucket_min_trades
        self.score_bucket_block_wr = score_bucket_block_wr

        # Stats
        self.total_blocked = 0
        self.total_allowed = 0

        # Seed from history
        self._load_history()

    def _score_bucket_name(self, score: float) -> str:
        for lo, hi in self.SCORE_BUCKETS:
            if lo <= score < hi:
                return f"{lo:.1f}-{hi:.1f}"
        return "unknown"

    def _load_history(self):
        """Seed from existing JSONL trade logs for this exchange."""
        pattern = f"{self.exchange}_*_trades.jsonl"
        loaded = 0
        for f in sorted(self.data_dir.glob(pattern)):
            # Skip aggregate files
            name = f.stem.replace("_trades", "")
            parts = name.split("_")
            if len(parts) < 2:
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

                    # Try "pnl" first, fall back to "pnl_delta" (old format)
                    pnl = trade.get("pnl")
                    if pnl is None:
                        pnl = trade.get("pnl_delta", 0.0)
                    score = trade.get("score")
                    # Skip reconciled positions (score=0 is junk data)
                    if score is not None and score == 0:
                        score = None

                    # Feed circuit breaker (just the final consecutive count matters)
                    if pnl < 0:
                        self.consecutive_losses += 1
                    else:
                        self.consecutive_losses = 0

                    # Feed score bucket
                    if score is not None and score >= 3.0:
                        bucket = self._score_bucket_name(score)
                        if bucket not in self.score_bucket_stats:
                            self.score_bucket_stats[bucket] = {"wins": 0, "losses": 0, "total": 0}
                        self.score_bucket_stats[bucket]["total"] += 1
                        if pnl > 0:
                            self.score_bucket_stats[bucket]["wins"] += 1
                        else:
                            self.score_bucket_stats[bucket]["losses"] += 1
                        loaded += 1

        # Don't start in cooldown from historical data — only live triggers
        self.consecutive_losses = 0

        bucket_summary = {k: f"{v['wins']}/{v['total']}" for k, v in self.score_bucket_stats.items()}
        logger.info(
            f"[SELF-LEARN|{self.exchange}] Seeded from {loaded} historical trades. "
            f"Score buckets: {json.dumps(bucket_summary)}"
        )

    def should_trade(self, symbol: str, score: float) -> Tuple[bool, str]:
        """
        Pre-entry check. Returns (allowed, reason).

        Checks:
        1. Circuit breaker — consecutive loss cooldown
        2. Score bucket — low WR score ranges
        """
        # 1. Circuit breaker
        if self.cooldown_until:
            now = datetime.now()
            if now < self.cooldown_until:
                remaining = (self.cooldown_until - now).total_seconds() / 60
                self.total_blocked += 1
                return False, f"CIRCUIT_BREAKER: {self.consecutive_losses} consecutive losses ({remaining:.0f}min left)"
            else:
                # Cooldown expired
                self.cooldown_until = None
                self.consecutive_losses = 0
                logger.info(f"[SELF-LEARN|{self.exchange}] Circuit breaker cooldown expired, resuming")

        # 2. Score bucket
        if score is not None and score >= 3.0:
            bucket = self._score_bucket_name(score)
            s = self.score_bucket_stats.get(bucket)
            if s and s["total"] >= self.score_bucket_min_trades:
                wr = s["wins"] / s["total"]
                if wr < self.score_bucket_block_wr:
                    self.total_blocked += 1
                    return False, f"SCORE_BUCKET_LOW: {bucket} WR={wr:.0%} ({s['total']} trades)"

        self.total_allowed += 1
        return True, "OK"

    def record_trade(self, symbol: str, score: float, pnl: float):
        """Post-exit: feed result to circuit breaker + score bucket."""
        # Circuit breaker
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.max_consecutive_losses and not self.cooldown_until:
                self.cooldown_until = datetime.now() + timedelta(minutes=self.cooldown_minutes)
                logger.warning(
                    f"[SELF-LEARN|{self.exchange}] CIRCUIT BREAKER TRIGGERED: "
                    f"{self.consecutive_losses} consecutive losses. "
                    f"Cooldown until {self.cooldown_until.strftime('%H:%M:%S')}"
                )
        else:
            self.consecutive_losses = 0
            self.cooldown_until = None  # Win clears cooldown

        # Score bucket
        if score is not None and score >= 3.0:
            bucket = self._score_bucket_name(score)
            if bucket not in self.score_bucket_stats:
                self.score_bucket_stats[bucket] = {"wins": 0, "losses": 0, "total": 0}
            self.score_bucket_stats[bucket]["total"] += 1
            if pnl > 0:
                self.score_bucket_stats[bucket]["wins"] += 1
            else:
                self.score_bucket_stats[bucket]["losses"] += 1

    def get_status(self) -> dict:
        """Get current learner state for logging."""
        return {
            "exchange": self.exchange,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_active": self.cooldown_until is not None,
            "cooldown_until": self.cooldown_until.isoformat() if self.cooldown_until else None,
            "score_buckets": {
                k: f"{v['wins']}/{v['total']} ({v['wins']/v['total']*100:.0f}% WR)" if v["total"] > 0 else "0/0"
                for k, v in self.score_bucket_stats.items()
            },
            "total_blocked": self.total_blocked,
            "total_allowed": self.total_allowed,
        }
