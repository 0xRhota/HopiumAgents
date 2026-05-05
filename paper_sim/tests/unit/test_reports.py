"""Tests for reports.py — paper ledger PnL reporting."""
from __future__ import annotations

import json

import pytest

from paper_sim.reports import report_for_account


def write_ledger(path, fills):
    with open(path, "w") as f:
        for fill in fills:
            f.write(json.dumps(fill) + "\n")


def make_record(**overrides):
    base = {
        "fill_id": "f", "ts_decision": 1.0, "ts_arrived": 1.2, "ts_filled": 2.0,
        "venue": "paradex", "symbol": "BTC-USD-PERP", "side": "BUY",
        "price": 100.0, "size": 1.0, "is_maker": True,
        "fee_bps": -0.5, "fee_paid_usd": -0.005,
        "funding_at_fill_bps": 0.0, "queue_ahead_at_arrival": 0.0,
        "strategy_tag": "", "adverse_drift_bps_t30": None,
        "account": "test",
    }
    base.update(overrides)
    return base


def test_empty_ledger(tmp_path):
    p = tmp_path / "missing_ledger.jsonl"
    rep = report_for_account(p)
    assert rep.fills == 0


def test_single_fill_open_position(tmp_path):
    p = tmp_path / "test_ledger.jsonl"
    write_ledger(p, [make_record(fill_id="a")])
    rep = report_for_account(p)
    assert rep.fills == 1
    assert rep.maker_fills == 1
    assert rep.realized_pnl_usd == 0  # still open
    assert rep.open_position_count == 1


def test_round_trip_realizes_pnl(tmp_path):
    p = tmp_path / "test_ledger.jsonl"
    fills = [
        make_record(fill_id="a", side="BUY", price=100.0, size=1.0),
        make_record(fill_id="b", side="SELL", price=105.0, size=1.0),
    ]
    write_ledger(p, fills)
    rep = report_for_account(p)
    assert rep.realized_pnl_usd == 5.0
    assert rep.open_position_count == 0


def test_partial_close_realizes_partial(tmp_path):
    p = tmp_path / "test_ledger.jsonl"
    fills = [
        make_record(fill_id="a", side="BUY", price=100.0, size=2.0),
        make_record(fill_id="b", side="SELL", price=105.0, size=1.0),
    ]
    write_ledger(p, fills)
    rep = report_for_account(p)
    assert rep.realized_pnl_usd == 5.0  # 1 unit × $5
    assert rep.open_position_count == 1


def test_fees_summed(tmp_path):
    p = tmp_path / "test_ledger.jsonl"
    fills = [
        make_record(fill_id="a", fee_paid_usd=-0.05),  # rebate
        make_record(fill_id="b", fee_paid_usd=0.10, is_maker=False),  # taker
    ]
    write_ledger(p, fills)
    rep = report_for_account(p)
    assert rep.cumulative_fees_usd == 0.05  # -0.05 + 0.10
    assert rep.maker_fills == 1
    assert rep.taker_fills == 1


def test_adverse_averaged(tmp_path):
    p = tmp_path / "test_ledger.jsonl"
    fills = [
        make_record(fill_id="a", adverse_drift_bps_t30=-3.0),
        make_record(fill_id="b", adverse_drift_bps_t30=-1.0),
        make_record(fill_id="c", adverse_drift_bps_t30=2.0),
        make_record(fill_id="d", adverse_drift_bps_t30=None),
    ]
    write_ledger(p, fills)
    rep = report_for_account(p)
    assert rep.cumulative_adverse_count == 3
    # mean = (-3 + -1 + 2) / 3 = -2/3
    assert abs(rep.cumulative_adverse_bps - (-2/3)) < 1e-9


def test_render_doesnt_crash(tmp_path):
    p = tmp_path / "test_ledger.jsonl"
    write_ledger(p, [make_record()])
    rep = report_for_account(p)
    rendered = rep.render()
    assert "Paper account" in rendered
    assert "Realized PnL" in rendered
