"""
Tests for Momentum Limit Order Strategy.

Unit tests (no network, no API keys):
- Trend detection with known candle data
- Entry/TP/SL price calculations
- Cooldown enforcement
- Balance-delta PnL logging

Integration tests (mocked exchange adapter):
- Full cycle: open → monitor → close
- Dry-run mode
"""

import asyncio
import csv
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from core.strategies.momentum.engine import MomentumEngine, MomentumConfig


# ─── Helpers ──────────────────────────────────────────────────────

def make_candles(
    n: int = 50,
    start_price: float = 100.0,
    trend: str = "up",
    volatility: float = 0.001,
) -> pd.DataFrame:
    """Generate synthetic OHLCV data for testing."""
    np.random.seed(42)
    prices = [start_price]

    for i in range(1, n):
        if trend == "up":
            drift = 0.002  # 0.2% per candle
        elif trend == "down":
            drift = -0.002
        else:  # sideways
            drift = 0.0

        noise = np.random.normal(0, volatility)
        new_price = prices[-1] * (1 + drift + noise)
        prices.append(new_price)

    df = pd.DataFrame({
        "timestamp": range(n),
        "open": [p * 0.999 for p in prices],
        "high": [p * 1.002 for p in prices],
        "low": [p * 0.998 for p in prices],
        "close": prices,
        "volume": [1000 + np.random.randint(0, 500) for _ in range(n)],
    })

    return df


# ─── Unit Tests: Engine ──────────────────────────────────────────

class TestDetectTrend:
    """Test trend detection with various market conditions."""

    def test_uptrend_produces_high_score(self):
        """Strong uptrend produces high score (signals present but may conflict)."""
        engine = MomentumEngine()
        df = make_candles(50, 100.0, "up")
        trend = engine.detect_trend(df)

        # v9 system: pure trends have conflicting signals (RSI overbought vs EMA bullish)
        # Score is high because individual signals are strong, but direction may be NONE
        assert trend["score"] >= 2.0
        assert trend["roc_bps"] > 0

    def test_downtrend_produces_score(self):
        """Strong downtrend produces meaningful score."""
        engine = MomentumEngine()
        df = make_candles(50, 100.0, "down")
        trend = engine.detect_trend(df)

        assert trend["score"] >= 1.0
        assert trend["roc_bps"] < 0

    def test_sideways_market(self):
        """Flat market should return NONE (low score)."""
        engine = MomentumEngine()
        df = make_candles(50, 100.0, "sideways", volatility=0.0001)
        trend = engine.detect_trend(df)

        # Sideways → score too low for 3.0 threshold
        assert trend["direction"] == "NONE"
        assert trend["score"] < 3.0

    def test_insufficient_data(self):
        """Less than 30 candles should return NONE."""
        engine = MomentumEngine()
        df = make_candles(15, 100.0, "up")
        trend = engine.detect_trend(df)

        assert trend["direction"] == "NONE"
        assert trend["score"] == 0.0

    def test_none_dataframe(self):
        """None input should return safe default."""
        engine = MomentumEngine()
        trend = engine.detect_trend(None)

        assert trend["direction"] == "NONE"

    def test_trend_contains_required_fields(self):
        """Trend dict should have all expected fields including v9 scoring."""
        engine = MomentumEngine()
        df = make_candles(50, 100.0, "up")
        trend = engine.detect_trend(df)

        assert "direction" in trend
        assert "score" in trend
        assert "strength" in trend
        assert "roc_bps" in trend
        assert "ema_diff_bps" in trend
        assert "rsi" in trend
        assert "vol_ratio" in trend
        assert "scoring" in trend

    def test_score_threshold_gates_direction(self):
        """Score below threshold should produce NONE regardless of signals."""
        engine = MomentumEngine(MomentumConfig(score_min=5.0))  # Impossible threshold
        df = make_candles(50, 100.0, "up")
        trend = engine.detect_trend(df)

        assert trend["direction"] == "NONE"
        assert trend["score"] > 0  # Signals exist but below threshold


class TestCalculateEntry:
    """Test limit order entry price calculations."""

    def test_long_entry_below_price(self):
        """LONG entry should be below current price."""
        engine = MomentumEngine(MomentumConfig(offset_bps=8.0))
        entry = engine.calculate_entry(100.0, "LONG")

        assert entry < 100.0
        assert entry == pytest.approx(99.92, abs=0.01)

    def test_short_entry_above_price(self):
        """SHORT entry should be above current price."""
        engine = MomentumEngine(MomentumConfig(offset_bps=8.0))
        entry = engine.calculate_entry(100.0, "SHORT")

        assert entry > 100.0
        assert entry == pytest.approx(100.08, abs=0.01)

    def test_hibachi_wider_offset(self):
        """Hibachi uses wider offset (18 bps)."""
        engine = MomentumEngine(MomentumConfig(offset_bps=18.0))
        entry = engine.calculate_entry(100.0, "LONG")

        assert entry == pytest.approx(99.82, abs=0.01)

    def test_none_direction(self):
        """NONE direction returns current price."""
        engine = MomentumEngine()
        assert engine.calculate_entry(100.0, "NONE") == 100.0

    def test_real_btc_price(self):
        """Test with realistic BTC price."""
        engine = MomentumEngine(MomentumConfig(offset_bps=8.0))
        entry = engine.calculate_entry(97500.0, "LONG")

        # Entry should be below current price by ~8 bps
        expected = 97500.0 * (1 - 8.0 / 10000)
        assert entry == pytest.approx(expected, abs=0.01)
        assert entry < 97500.0


class TestTpSl:
    """Test take-profit / stop-loss calculations."""

    def test_long_tp_above_entry(self):
        """LONG TP should be above entry."""
        engine = MomentumEngine(MomentumConfig(tp_bps=40.0, sl_bps=25.0))
        tp, sl = engine.calculate_tp_sl(100.0, "LONG")

        assert tp > 100.0
        assert sl < 100.0
        assert tp == pytest.approx(100.40, abs=0.01)
        assert sl == pytest.approx(99.75, abs=0.01)

    def test_short_tp_below_entry(self):
        """SHORT TP should be below entry."""
        engine = MomentumEngine(MomentumConfig(tp_bps=40.0, sl_bps=25.0))
        tp, sl = engine.calculate_tp_sl(100.0, "SHORT")

        assert tp < 100.0
        assert sl > 100.0
        assert tp == pytest.approx(99.60, abs=0.01)
        assert sl == pytest.approx(100.25, abs=0.01)

    def test_asymmetric_rr(self):
        """TP should be wider than SL (positive R:R)."""
        engine = MomentumEngine(MomentumConfig(tp_bps=40.0, sl_bps=25.0))
        tp, sl = engine.calculate_tp_sl(100.0, "LONG")

        tp_distance = abs(tp - 100.0)
        sl_distance = abs(sl - 100.0)
        assert tp_distance > sl_distance


class TestCooldown:
    """Test post-close cooldown enforcement."""

    def test_no_cooldown_initially(self):
        engine = MomentumEngine()
        assert not engine.in_cooldown()

    def test_cooldown_after_close(self):
        engine = MomentumEngine(MomentumConfig(cooldown_seconds=300))
        engine.record_close()
        assert engine.in_cooldown()

    def test_cooldown_expires(self):
        engine = MomentumEngine(MomentumConfig(cooldown_seconds=1))
        engine.record_close()
        time.sleep(1.1)
        assert not engine.in_cooldown()


class TestShouldExit:
    """Test exit condition detection."""

    def test_long_tp_hit(self):
        engine = MomentumEngine(MomentumConfig(tp_bps=40.0, sl_bps=25.0))
        entry_time = time.time()
        result = engine.should_exit(100.50, 100.0, "LONG", entry_time)
        assert result == "TP"

    def test_long_sl_hit(self):
        engine = MomentumEngine(MomentumConfig(tp_bps=40.0, sl_bps=25.0))
        entry_time = time.time()
        result = engine.should_exit(99.70, 100.0, "LONG", entry_time)
        assert result == "SL"

    def test_short_tp_hit(self):
        engine = MomentumEngine(MomentumConfig(tp_bps=40.0, sl_bps=25.0))
        entry_time = time.time()
        result = engine.should_exit(99.50, 100.0, "SHORT", entry_time)
        assert result == "TP"

    def test_short_sl_hit(self):
        engine = MomentumEngine(MomentumConfig(tp_bps=40.0, sl_bps=25.0))
        entry_time = time.time()
        result = engine.should_exit(100.30, 100.0, "SHORT", entry_time)
        assert result == "SL"

    def test_no_exit_in_range(self):
        engine = MomentumEngine(MomentumConfig(tp_bps=40.0, sl_bps=25.0))
        entry_time = time.time()
        result = engine.should_exit(100.10, 100.0, "LONG", entry_time)
        assert result is None

    def test_time_exit(self):
        engine = MomentumEngine(MomentumConfig(max_hold_minutes=60))
        entry_time = time.time() - 3700  # 61+ minutes ago
        result = engine.should_exit(100.10, 100.0, "LONG", entry_time)
        assert result == "TIME"


# ─── Integration Tests: Balance-Delta PnL ────────────────────────

class TestBalanceDeltaPnL:
    """Test that PnL is tracked via equity snapshots, not price math."""

    def test_audit_csv_written(self):
        """Verify audit CSV gets equity snapshots."""
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            filepath = Path(f.name)

        try:
            from scripts.momentum_mm import append_csv

            append_csv(filepath, {
                "timestamp": "2026-02-09T12:00:00Z",
                "equity": 81.94,
                "event": "PRE_OPEN",
                "cycle": 1,
                "exchange": "nado",
                "asset": "BTC",
            })
            append_csv(filepath, {
                "timestamp": "2026-02-09T12:15:00Z",
                "equity": 82.13,
                "event": "POST_CLOSE",
                "cycle": 15,
                "exchange": "nado",
                "asset": "BTC",
            })

            # Read back and verify
            with open(filepath) as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            assert len(rows) == 2
            assert float(rows[0]["equity"]) == 81.94
            assert rows[0]["event"] == "PRE_OPEN"
            assert float(rows[1]["equity"]) == 82.13
            assert rows[1]["event"] == "POST_CLOSE"

            # Verify PnL delta
            pnl = float(rows[1]["equity"]) - float(rows[0]["equity"])
            assert pnl == pytest.approx(0.19, abs=0.01)

        finally:
            os.unlink(filepath)

    def test_trade_jsonl_written(self):
        """Verify trade JSONL records equity-based PnL."""
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
            filepath = Path(f.name)

        try:
            from scripts.momentum_mm import append_jsonl

            trade = {
                "id": "test123",
                "exchange": "nado",
                "symbol": "BTC",
                "side": "LONG",
                "equity_before": 81.94,
                "equity_after": 82.13,
                "pnl_delta": 0.19,
                "exit_reason": "TP",
            }
            append_jsonl(filepath, trade)

            with open(filepath) as f:
                record = json.loads(f.readline())

            assert record["pnl_delta"] == 0.19
            assert record["equity_before"] == 81.94
            assert record["equity_after"] == 82.13
            assert "_timestamp" in record  # Auto-added

        finally:
            os.unlink(filepath)


# ─── Integration Tests: Mocked Exchange ──────────────────────────

class TestDryRun:
    """Test that dry-run mode doesn't place real orders."""

    @pytest.fixture(autouse=True)
    def setup_symbols(self):
        """Populate EXCHANGE_SYMBOLS for dry-run tests."""
        from scripts.momentum_mm import EXCHANGE_SYMBOLS, BINANCE_SYMBOLS
        EXCHANGE_SYMBOLS["nado"] = {"BTC": "BTC-PERP", "ETH": "ETH-PERP"}
        EXCHANGE_SYMBOLS["hibachi"] = {"BTC": "BTC/USDT-P", "ETH": "ETH/USDT-P"}
        EXCHANGE_SYMBOLS["extended"] = {"BTC": "BTC", "ETH": "ETH"}
        BINANCE_SYMBOLS["BTC"] = "BTCUSDT"
        BINANCE_SYMBOLS["ETH"] = "ETHUSDT"
        yield
        # Cleanup
        EXCHANGE_SYMBOLS.clear()
        BINANCE_SYMBOLS.clear()

    def test_dry_run_creates_no_adapter(self):
        """Dry-run should have adapter=None."""
        from scripts.momentum_mm import MomentumBot

        bot = MomentumBot(
            exchange="nado",
            asset="BTC",
            dry_run=True,
        )
        assert bot.adapter is None
        assert bot.dry_run is True

    def test_dry_run_symbol_mapping(self):
        """Symbol should be correctly mapped per exchange."""
        from scripts.momentum_mm import MomentumBot

        bot_nado = MomentumBot(exchange="nado", asset="BTC", dry_run=True)
        assert bot_nado.symbol == "BTC-PERP"

        bot_hibachi = MomentumBot(exchange="hibachi", asset="BTC", dry_run=True)
        assert bot_hibachi.symbol == "BTC/USDT-P"

        bot_ext = MomentumBot(exchange="extended", asset="ETH", dry_run=True)
        assert bot_ext.symbol == "ETH"

    def test_hibachi_uses_wider_offset(self):
        """Hibachi should use 18 bps offset (no POST_ONLY)."""
        from scripts.momentum_mm import MomentumBot

        bot = MomentumBot(exchange="hibachi", asset="BTC", dry_run=True)
        assert bot.engine.config.offset_bps == 18.0

    def test_nado_uses_default_offset(self):
        """Nado should use 8 bps offset (with POST_ONLY)."""
        from scripts.momentum_mm import MomentumBot

        bot = MomentumBot(exchange="nado", asset="BTC", dry_run=True)
        assert bot.engine.config.offset_bps == 8.0


class TestMomentumConfig:
    """Test configuration defaults and constraints."""

    def test_default_config(self):
        config = MomentumConfig()
        assert config.offset_bps == 8.0
        assert config.tp_bps == 150.0
        assert config.sl_bps == 100.0
        assert config.size_usd == 50.0
        assert config.score_min == 3.0
        assert config.max_positions == 5
        assert config.cooldown_seconds == 300
        assert config.max_hold_minutes == 120

    def test_no_martingale(self):
        """Size should be fixed, never scaled."""
        config = MomentumConfig()
        # Size is a fixed value, no multiplier or scaling field
        assert config.size_usd == 50.0
        # There should be no confidence_multiplier or scale_factor
        assert not hasattr(config, 'confidence_multiplier')
        assert not hasattr(config, 'scale_factor')
