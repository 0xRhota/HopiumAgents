"""Append-only paper ledger.

Mirrors the contract of `core/reconciliation/ledger.py` in the live system —
JSONL on disk, fsync per write, dedup by fill_id. Format compatible with
`scripts/real_pnl.py` so the same reporter can read paper output.
"""
from __future__ import annotations

import json
import os
import dataclasses
from pathlib import Path
from typing import Iterator, Optional, Set

from paper_sim.core.types import PaperFill


class PaperLedger:
    """Append-only JSONL ledger for paper fills.

    One ledger per (account). Lines are JSON-serialized PaperFill records.
    Dedup is by fill_id — re-appending an already-seen id is a no-op.
    """

    def __init__(self, path: str | Path, account: str):
        self.path = Path(path)
        self.account = account
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seen_ids: Set[str] = set()
        # Pre-populate seen_ids from any existing file
        if self.path.exists():
            for fill in self.read_all():
                self._seen_ids.add(fill.fill_id)

    def append(self, fill: PaperFill) -> bool:
        """Append a fill. Returns True if written, False if duplicate."""
        if fill.fill_id in self._seen_ids:
            return False
        record = dataclasses.asdict(fill)
        record["account"] = self.account
        line = json.dumps(record, sort_keys=True)
        with open(self.path, "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._seen_ids.add(fill.fill_id)
        return True

    def update_adverse(self, fill_id: str, drift_bps: float) -> bool:
        """Annotate a previously-written fill with its adverse selection drift.

        Implementation: rewrites the file in place. Cheap for our volumes
        (paper week ~1k-5k fills total).
        """
        if fill_id not in self._seen_ids:
            return False
        if not self.path.exists():
            return False

        all_fills = list(self.read_all_raw())
        updated = False
        for r in all_fills:
            if r.get("fill_id") == fill_id:
                r["adverse_drift_bps_t30"] = drift_bps
                updated = True
                break
        if not updated:
            return False

        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w") as f:
            for r in all_fills:
                f.write(json.dumps(r, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        return True

    def read_all(self) -> Iterator[PaperFill]:
        """Iterate over all fills as PaperFill instances."""
        for r in self.read_all_raw():
            r.pop("account", None)  # not part of dataclass
            yield PaperFill(**r)

    def read_all_raw(self) -> Iterator[dict]:
        """Iterate over all lines as raw dicts (preserves unknown fields)."""
        if not self.path.exists():
            return
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)

    def count(self) -> int:
        return len(self._seen_ids)
