"""LLM decision log — captures every briefing, raw response, parsed ideas,
consensus result, and orders placed for each Account C cycle.

Mirrors the live-trading-bot decision-log pattern. One JSONL per account at
`logs/paper/{account}_decisions.jsonl`. Append-only, fsync per write.

Two record types interleaved:
  - {type: "llm_call",  ...}  one per LLM per cycle
  - {type: "consensus", ...}  one per cycle, after consensus computed
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path
from typing import List, Optional


class DecisionLog:
    def __init__(self, path: str | Path, account: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.account = account

    def _append(self, rec: dict) -> None:
        rec.setdefault("ts", time.time())
        rec.setdefault("account", self.account)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def append_llm_call(self, briefing: dict, model: str,
                        raw_response: Optional[str], parsed_ideas: List,
                        error: Optional[str], cycle_id: str) -> None:
        self._append({
            "type": "llm_call",
            "cycle_id": cycle_id,
            "model": model,
            "briefing": briefing,
            "raw_response": raw_response,
            "parsed_ideas": [_idea_to_dict(i) for i in parsed_ideas],
            "error": error,
        })

    def append_consensus(self, briefing_ts: float, cycle_id: str,
                         consensus_ideas: List, orders_placed: List) -> None:
        self._append({
            "type": "consensus",
            "cycle_id": cycle_id,
            "briefing_ts": briefing_ts,
            "consensus_ideas": [_idea_to_dict(i) for i in consensus_ideas],
            "orders_placed": [_order_to_dict(o) for o in orders_placed],
        })


def _idea_to_dict(i) -> dict:
    if dataclasses.is_dataclass(i):
        return dataclasses.asdict(i)
    return dict(i) if isinstance(i, dict) else {"raw": str(i)}


def _order_to_dict(o) -> dict:
    if dataclasses.is_dataclass(o):
        return dataclasses.asdict(o)
    return dict(o) if isinstance(o, dict) else {"raw": str(o)}
