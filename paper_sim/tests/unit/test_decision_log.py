"""Tests for core/decision_log.py — LLM decision capture."""
from __future__ import annotations

import json

import pytest

from paper_sim.core.decision_log import DecisionLog
from paper_sim.strategies.account_c_llm_scout import LLMTradeIdea


def test_append_llm_call(tmp_path):
    log = DecisionLog(tmp_path / "C1_decisions.jsonl", account="C1")
    log.append_llm_call(
        briefing={"ts": 1.0, "btc_regime": "TREND_UP"},
        model="anthropic/claude-opus",
        raw_response='[{"symbol":"BTC","direction":"LONG","conviction":7,"thesis":"x","time_horizon_hours":24}]',
        parsed_ideas=[LLMTradeIdea("BTC", "LONG", 7, "x", 24)],
        error=None, cycle_id="c1",
    )
    lines = (tmp_path / "C1_decisions.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["type"] == "llm_call"
    assert rec["model"] == "anthropic/claude-opus"
    assert rec["briefing"]["btc_regime"] == "TREND_UP"
    assert rec["raw_response"].startswith("[{")
    assert len(rec["parsed_ideas"]) == 1
    assert rec["parsed_ideas"][0]["symbol"] == "BTC"


def test_append_consensus(tmp_path):
    log = DecisionLog(tmp_path / "C1_decisions.jsonl", account="C1")
    ideas = [LLMTradeIdea("BTC", "LONG", 7, "consensus", 24)]
    log.append_consensus(briefing_ts=1.0, cycle_id="c1",
                         consensus_ideas=ideas, orders_placed=[])
    rec = json.loads((tmp_path / "C1_decisions.jsonl").read_text().strip())
    assert rec["type"] == "consensus"
    assert rec["cycle_id"] == "c1"
    assert rec["consensus_ideas"][0]["symbol"] == "BTC"


def test_error_path_logged(tmp_path):
    log = DecisionLog(tmp_path / "C1_decisions.jsonl", account="C1")
    log.append_llm_call(
        briefing={"ts": 1.0}, model="x", raw_response=None,
        parsed_ideas=[], error="no_response", cycle_id="c1",
    )
    rec = json.loads((tmp_path / "C1_decisions.jsonl").read_text().strip())
    assert rec["error"] == "no_response"
    assert rec["raw_response"] is None
