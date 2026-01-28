"""
Circuit Breaker - Prevents catastrophic loss sequences

Triggers:
- 5 consecutive losses (regardless of wins between)
- 5% rolling 24h drawdown

Cooldown:
- 1 hour minimum
- Can override once at 50% size if high conviction
- Extended cooldown if override trade loses
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CooldownState:
    """State during cooldown period"""
    triggered_at: datetime
    trigger_reason: str
    override_used: bool
    cooldown_until: datetime


class CircuitBreaker:
    """
    Prevents trading during adverse conditions.

    Monitors:
    - Consecutive losses
    - Rolling drawdown
    - Time-based cooldowns
    """

    def __init__(
        self,
        max_consecutive_losses: int = 5,
        max_daily_drawdown_pct: float = 5.0,
        cooldown_minutes: int = 60,
        override_size_multiplier: float = 0.5,
    ):
        """
        Initialize circuit breaker.

        Args:
            max_consecutive_losses: Losses before triggering (default 5)
            max_daily_drawdown_pct: Max 24h drawdown before triggering (default 5%)
            cooldown_minutes: Cooldown duration in minutes (default 60)
            override_size_multiplier: Size for override trades (default 0.5)
        """
        self.max_consecutive_losses = max_consecutive_losses
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.cooldown_minutes = cooldown_minutes
        self.override_size_multiplier = override_size_multiplier

        # State
        self.consecutive_losses = 0
        self.total_losses = 0  # For the Qwen "4+win+3" rule
        self.trade_history: List[Dict] = []  # [{pnl, timestamp}]
        self.cooldown_state: Optional[CooldownState] = None

        # Statistics
        self.total_triggers = 0
        self.successful_overrides = 0
        self.failed_overrides = 0

    def record_trade(self, pnl: float):
        """
        Record trade result.

        Args:
            pnl: Trade P&L in USD (positive = win, negative = loss)
        """
        now = datetime.now()

        # Record in history
        self.trade_history.append({
            'pnl': pnl,
            'timestamp': now.isoformat()
        })

        # Keep only last 24h of trades
        cutoff = now - timedelta(hours=24)
        self.trade_history = [
            t for t in self.trade_history
            if datetime.fromisoformat(t['timestamp']) > cutoff
        ]

        # Update consecutive loss counter
        if pnl < 0:
            self.consecutive_losses += 1
            self.total_losses += 1
            logger.warning(f"[CIRCUIT] Loss recorded. Consecutive: {self.consecutive_losses}")
        else:
            # Win resets consecutive but not total (Qwen rule)
            self.consecutive_losses = 0
            self.total_losses = 0
            logger.info(f"[CIRCUIT] Win recorded. Consecutive losses reset.")

        # Check triggers
        self._check_triggers()

    def _check_triggers(self):
        """Check if circuit breaker should trigger."""
        triggered = False
        reason = ""

        # Check consecutive losses
        if self.consecutive_losses >= self.max_consecutive_losses:
            triggered = True
            reason = f"{self.consecutive_losses} consecutive losses"

        # Check total losses (Qwen 4+win+3 rule: 7 total)
        if self.total_losses >= 7:
            triggered = True
            reason = f"{self.total_losses} losses (including win breaks)"

        # Check daily drawdown
        drawdown = self._calculate_daily_drawdown()
        if drawdown >= self.max_daily_drawdown_pct:
            triggered = True
            reason = f"{drawdown:.1f}% daily drawdown"

        if triggered:
            self._trigger_cooldown(reason)

    def _calculate_daily_drawdown(self) -> float:
        """Calculate rolling 24h drawdown percentage."""
        if not self.trade_history:
            return 0.0

        # Sum all P&L in last 24h
        total_pnl = sum(t['pnl'] for t in self.trade_history)

        # Calculate as percentage of account
        # Assume $100 starting capital (adjustable)
        base_capital = 100.0
        drawdown_pct = abs(min(0, total_pnl)) / base_capital * 100

        return drawdown_pct

    def _trigger_cooldown(self, reason: str):
        """Trigger cooldown period."""
        now = datetime.now()
        cooldown_until = now + timedelta(minutes=self.cooldown_minutes)

        self.cooldown_state = CooldownState(
            triggered_at=now,
            trigger_reason=reason,
            override_used=False,
            cooldown_until=cooldown_until
        )

        self.total_triggers += 1

        logger.warning("=" * 60)
        logger.warning(f"[CIRCUIT] TRIGGERED: {reason}")
        logger.warning(f"[CIRCUIT] Cooldown until: {cooldown_until.strftime('%H:%M:%S')}")
        logger.warning("=" * 60)

    def is_triggered(self) -> Tuple[bool, str]:
        """
        Check if circuit breaker is currently active.

        Returns:
            (is_triggered: bool, reason: str)
        """
        if not self.cooldown_state:
            return False, ""

        now = datetime.now()

        # Check if cooldown expired
        if now >= self.cooldown_state.cooldown_until:
            logger.info("[CIRCUIT] Cooldown expired, resuming normal trading")
            self._reset()
            return False, ""

        remaining = (self.cooldown_state.cooldown_until - now).total_seconds() / 60
        return True, f"{self.cooldown_state.trigger_reason} ({remaining:.0f}min remaining)"

    def should_allow_override(self, confidence: float, regime: str) -> Tuple[bool, float]:
        """
        Check if a high-conviction trade should override cooldown.

        Based on Qwen: Allow ONE override at 50% size, extend cooldown if loses.

        Args:
            confidence: Calibrated confidence
            regime: Current market regime

        Returns:
            (allow: bool, size_multiplier: float)
        """
        if not self.cooldown_state:
            return True, 1.0  # Not in cooldown

        # Check if override already used
        if self.cooldown_state.override_used:
            logger.warning("[CIRCUIT] Override already used this cooldown")
            return False, 0.0

        # Require high confidence for override
        if confidence < 0.55:
            logger.info(f"[CIRCUIT] Override denied: confidence {confidence:.2f} < 0.55")
            return False, 0.0

        # Don't override in choppy/range-bound regimes
        if regime in ['CHOPPY', 'RANGE_BOUND']:
            logger.info(f"[CIRCUIT] Override denied: unfavorable regime {regime}")
            return False, 0.0

        # Allow override
        logger.warning(f"[CIRCUIT] Override ALLOWED at {self.override_size_multiplier}x size")
        self.cooldown_state.override_used = True
        return True, self.override_size_multiplier

    def record_override_result(self, won: bool):
        """
        Record result of override trade.

        If lost, extend cooldown per Qwen recommendation.

        Args:
            won: Whether override trade was profitable
        """
        if not self.cooldown_state:
            return

        if won:
            self.successful_overrides += 1
            logger.info("[CIRCUIT] Override trade WON")
        else:
            self.failed_overrides += 1
            # Extend cooldown by 1 hour
            extension = timedelta(hours=1)
            self.cooldown_state.cooldown_until += extension
            logger.warning(f"[CIRCUIT] Override trade LOST - cooldown extended by 1h")
            logger.warning(f"[CIRCUIT] New cooldown until: {self.cooldown_state.cooldown_until.strftime('%H:%M:%S')}")

    def _reset(self):
        """Reset circuit breaker state after cooldown."""
        self.consecutive_losses = 0
        self.total_losses = 0
        self.cooldown_state = None
        logger.info("[CIRCUIT] Circuit breaker reset")

    def force_reset(self):
        """Force reset circuit breaker (manual override)."""
        logger.warning("[CIRCUIT] FORCE RESET by user")
        self._reset()

    def get_status(self) -> Dict:
        """Get current circuit breaker status."""
        is_active, reason = self.is_triggered()
        return {
            'is_active': is_active,
            'reason': reason,
            'consecutive_losses': self.consecutive_losses,
            'total_losses': self.total_losses,
            'daily_drawdown_pct': self._calculate_daily_drawdown(),
            'total_triggers': self.total_triggers,
            'cooldown_until': self.cooldown_state.cooldown_until.isoformat() if self.cooldown_state else None,
            'override_available': self.cooldown_state and not self.cooldown_state.override_used if self.cooldown_state else False
        }

    def get_prompt_context(self) -> str:
        """Get circuit breaker context for LLM prompt."""
        is_active, reason = self.is_triggered()

        if is_active:
            status = self.get_status()
            return (
                f"\n=== CIRCUIT BREAKER ACTIVE ===\n"
                f"Reason: {reason}\n"
                f"Override available: {status['override_available']}\n"
                f"IMPORTANT: Only trade if high conviction (>0.55 calibrated confidence)\n"
            )
        else:
            drawdown = self._calculate_daily_drawdown()
            return (
                f"\n=== CIRCUIT BREAKER STATUS ===\n"
                f"Status: Normal trading\n"
                f"Consecutive losses: {self.consecutive_losses}/{self.max_consecutive_losses}\n"
                f"Daily drawdown: {drawdown:.1f}%/{self.max_daily_drawdown_pct}%\n"
            )
