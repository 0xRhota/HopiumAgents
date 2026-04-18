"""Append-only persistent ledger of verified fills.

This is the ONLY place PnL data is stored after reconciliation.
It is NEVER computed from bot state — only from exchange fills.

File format: newline-delimited JSON. One Fill per line.
Keyed by (exchange, fill_id). Duplicates raise DuplicateFillError.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from core.reconciliation.base import Fill


class DuplicateFillError(Exception):
    """Raised when attempting to append a (exchange, fill_id) that already exists."""


class Ledger:
    """Append-only JSONL store of fills.

    Concurrency note: single-writer assumed. If multiple reconcilers
    need to write, wrap append() in a file lock. For now each bot
    cycle is single-threaded.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen: Set[Tuple[str, str]] = set()  # (exchange, fill_id)
        self._load_keys()

    def _load_keys(self) -> None:
        if not self.path.exists():
            return
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                self._seen.add((d["exchange"], d["fill_id"]))

    # ── Mutation ─────────────────────────────────────────────────────

    def append(self, fill: Fill) -> None:
        """Append a fill. Raises DuplicateFillError if (exchange, fill_id) already recorded."""
        key = (fill.exchange, fill.fill_id)
        if key in self._seen:
            raise DuplicateFillError(f"Fill already in ledger: {key}")
        line = json.dumps(fill.to_dict()) + "\n"
        with self.path.open("a") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        self._seen.add(key)

    # ── Read / Query ─────────────────────────────────────────────────

    def _iter_fills(self):
        if not self.path.exists():
            return
        with self.path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield Fill.from_dict(json.loads(line))

    def all(self) -> List[Fill]:
        return list(self._iter_fills())

    def count(self) -> int:
        return len(self._seen)

    def fills_by_exchange(self, exchange: str) -> List[Fill]:
        return [f for f in self._iter_fills() if f.exchange == exchange]

    def fills_in_window(self, start: datetime, end: datetime,
                        exchange: Optional[str] = None) -> List[Fill]:
        out = []
        for f in self._iter_fills():
            if exchange and f.exchange != exchange:
                continue
            if start <= f.ts < end:
                out.append(f)
        return out

    def total_pnl_net(self, exchange: Optional[str] = None) -> float:
        """Sum of (realized - fee) across all fills.

        OPEN fills contribute -fee (fee is a cost regardless of realization).
        CLOSE fills contribute (realized - fee).
        This matches exchange equity delta when integrated over all fills.
        """
        total = 0.0
        for f in self._iter_fills():
            if exchange and f.exchange != exchange:
                continue
            if f.realized_pnl_usd is None:
                total -= f.fee
            else:
                total += f.realized_pnl_usd - f.fee
        return total

    def unreconciled_opens(self, exchange: Optional[str] = None) -> List[Fill]:
        """OPEN fills that have no matching CLOSE fill referencing them."""
        opens: Dict[str, Fill] = {}
        closed_refs: Set[str] = set()
        for f in self._iter_fills():
            if exchange and f.exchange != exchange:
                continue
            if f.opens_or_closes == "OPEN":
                opens[f.fill_id] = f
            elif f.linked_entry_fill_id:
                closed_refs.add(f.linked_entry_fill_id)
        return [f for fid, f in opens.items() if fid not in closed_refs]
