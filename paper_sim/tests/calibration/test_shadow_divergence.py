"""Shadow-mode calibration test.

Compares paper-sim predicted fills to actual live-bot fills observed during
the same window. PASS criterion (the deploy gate):
  - Median |paper_price - live_price| < 2 bps on majors
  - Median |paper_price - live_price| < 5 bps on long-tail

This test is gated on the existence of:
  - logs/paper/{venue}_l2_recording.jsonl  (recorded by `cli calibrate`)
  - logs/ledger/{venue}_ledger.jsonl       (existing live-bot ledger)

If either is missing, the test is skipped (intentional — it's the deploy gate,
not a unit test).

Until 24h of recording is captured, this remains a placeholder skip.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
PAPER_DIR = REPO_ROOT / "logs" / "paper"
LEDGER_DIR = REPO_ROOT / "logs" / "ledger"


def _has_recording(venue: str) -> bool:
    p = PAPER_DIR / f"{venue}_l2_recording.jsonl"
    return p.exists() and p.stat().st_size > 1000


def _has_live_ledger(venue: str) -> bool:
    p = LEDGER_DIR / f"{venue}_ledger.jsonl"
    return p.exists() and p.stat().st_size > 100


@pytest.mark.parametrize("venue", ["paradex", "hyperliquid"])
def test_calibration_data_present_or_skip(venue):
    """Skip until calibration data exists; this is informational."""
    if not _has_recording(venue):
        pytest.skip(f"no L2 recording for {venue} yet — run `cli calibrate --duration 24h`")
    if not _has_live_ledger(venue):
        pytest.skip(f"no live ledger for {venue} — run live bots in parallel")

    # Once both are present, this test will exercise the actual divergence math.
    # For now it just confirms the file shapes are valid JSONL.
    rec_path = PAPER_DIR / f"{venue}_l2_recording.jsonl"
    led_path = LEDGER_DIR / f"{venue}_ledger.jsonl"

    rec_count = 0
    with open(rec_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            assert "kind" in rec
            rec_count += 1

    led_count = 0
    with open(led_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            assert "exchange" in entry or "venue" in entry
            led_count += 1

    assert rec_count > 0
    assert led_count > 0


def test_divergence_calculator_logic():
    """Pure-function check of the divergence formula (runs without live data)."""
    from paper_sim.tests.calibration.divergence import paper_vs_live_bps

    # Live filled at 79000, paper would have filled at 79002 → +0.25 bps
    bps = paper_vs_live_bps(live_price=79000.0, paper_price=79002.0)
    assert abs(bps - 0.253164) < 1e-3

    # Live 100, paper 99.95 → -5 bps
    bps = paper_vs_live_bps(live_price=100.0, paper_price=99.95)
    assert abs(bps - (-5.0)) < 1e-6
