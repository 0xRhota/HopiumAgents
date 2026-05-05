"""Tests for core/ledger.py — append-only paper ledger."""
from __future__ import annotations

import json
import pytest

from paper_sim.core.ledger import PaperLedger
from paper_sim.core.types import PaperFill


def make_fill(fill_id: str = "f1", **overrides) -> PaperFill:
    base = dict(
        fill_id=fill_id, ts_decision=1.0, ts_arrived=1.2, ts_filled=2.0,
        venue="paradex", symbol="BTC-USD-PERP", side="BUY", price=80000.0,
        size=0.001, is_maker=True, fee_bps=-0.5, fee_paid_usd=-0.04,
        funding_at_fill_bps=1.0, queue_ahead_at_arrival=5.0,
        strategy_tag="A", adverse_drift_bps_t30=None,
    )
    base.update(overrides)
    return PaperFill(**base)


@pytest.fixture
def ledger_path(tmp_path):
    return tmp_path / "test_ledger.jsonl"


class TestAppend:
    def test_first_append_writes(self, ledger_path):
        led = PaperLedger(ledger_path, account="A")
        assert led.append(make_fill("a")) is True
        assert ledger_path.exists()
        assert led.count() == 1

    def test_dedup_same_id(self, ledger_path):
        led = PaperLedger(ledger_path, account="A")
        led.append(make_fill("a"))
        assert led.append(make_fill("a", price=99999.0)) is False
        assert led.count() == 1

    def test_multiple_distinct(self, ledger_path):
        led = PaperLedger(ledger_path, account="A")
        led.append(make_fill("a"))
        led.append(make_fill("b"))
        led.append(make_fill("c"))
        assert led.count() == 3

    def test_account_in_record(self, ledger_path):
        led = PaperLedger(ledger_path, account="A")
        led.append(make_fill("a"))
        with open(ledger_path) as f:
            rec = json.loads(f.readline())
        assert rec["account"] == "A"


class TestPersistence:
    def test_reload_repopulates_seen_ids(self, ledger_path):
        led1 = PaperLedger(ledger_path, account="A")
        led1.append(make_fill("a"))
        led1.append(make_fill("b"))

        led2 = PaperLedger(ledger_path, account="A")
        assert led2.count() == 2
        # Re-attempt to append known id → no-op
        assert led2.append(make_fill("a")) is False
        assert led2.append(make_fill("c")) is True


class TestRead:
    def test_read_all(self, ledger_path):
        led = PaperLedger(ledger_path, account="A")
        led.append(make_fill("a", price=100.0))
        led.append(make_fill("b", price=101.0))

        fills = list(led.read_all())
        assert len(fills) == 2
        assert {f.fill_id for f in fills} == {"a", "b"}

    def test_read_all_empty(self, tmp_path):
        led = PaperLedger(tmp_path / "empty.jsonl", account="A")
        assert list(led.read_all()) == []


class TestAdverseUpdate:
    def test_update_existing_fill(self, ledger_path):
        led = PaperLedger(ledger_path, account="A")
        led.append(make_fill("a"))
        assert led.update_adverse("a", -3.5) is True

        fills = list(led.read_all())
        assert fills[0].adverse_drift_bps_t30 == -3.5

    def test_update_nonexistent(self, ledger_path):
        led = PaperLedger(ledger_path, account="A")
        led.append(make_fill("a"))
        assert led.update_adverse("nope", 1.0) is False

    def test_update_preserves_other_fills(self, ledger_path):
        led = PaperLedger(ledger_path, account="A")
        led.append(make_fill("a", price=100.0))
        led.append(make_fill("b", price=200.0))
        led.append(make_fill("c", price=300.0))

        led.update_adverse("b", 5.0)

        fills = sorted(led.read_all(), key=lambda f: f.price)
        assert fills[0].fill_id == "a"
        assert fills[0].adverse_drift_bps_t30 is None
        assert fills[1].fill_id == "b"
        assert fills[1].adverse_drift_bps_t30 == 5.0
        assert fills[2].fill_id == "c"
        assert fills[2].adverse_drift_bps_t30 is None
