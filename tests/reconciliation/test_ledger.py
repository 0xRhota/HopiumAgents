"""Tests for the append-only Ledger store.

Ledger invariants:
- Append-only: no file can be rewritten, only appended to
- Fills keyed by (exchange, fill_id) — duplicate inserts raise
- Query helpers: fills_in_window, total_pnl_net, unreconciled_opens
- Survives crashes: every fill is fsynced before the method returns
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from core.reconciliation.base import Fill
from core.reconciliation.ledger import Ledger, DuplicateFillError


@pytest.fixture
def tmp_ledger(tmp_path):
    return Ledger(tmp_path / "test_ledger.jsonl")


def _mk_fill(i, exchange="nado", pnl=None, close=False, fee=0.04, ts=None, linked=None):
    return Fill(
        exchange=exchange, symbol="LIT-PERP",
        fill_id=f"fill-{i}", order_id=f"ord-{i}",
        ts=ts or datetime(2026, 4, 17, 12, i, tzinfo=timezone.utc),
        side="SELL" if close else "BUY", size=100.0,
        price=1.04 if close else 1.02,
        fee=fee, is_maker=True,
        realized_pnl_usd=pnl, linked_entry_fill_id=linked,
        opens_or_closes="CLOSE" if close else "OPEN",
    )


# ── Append ───────────────────────────────────────────────────────────

def test_append_fill_creates_file(tmp_ledger):
    f = _mk_fill(1)
    tmp_ledger.append(f)
    assert tmp_ledger.path.exists()


def test_append_writes_jsonl(tmp_ledger):
    f = _mk_fill(1)
    tmp_ledger.append(f)
    lines = tmp_ledger.path.read_text().strip().split("\n")
    assert len(lines) == 1
    d = json.loads(lines[0])
    assert d["fill_id"] == "fill-1"


def test_append_duplicate_raises(tmp_ledger):
    f = _mk_fill(1)
    tmp_ledger.append(f)
    with pytest.raises(DuplicateFillError):
        tmp_ledger.append(f)


def test_append_different_exchanges_same_fill_id_ok(tmp_ledger):
    """Same fill_id allowed on different exchanges."""
    tmp_ledger.append(_mk_fill(1, exchange="nado"))
    tmp_ledger.append(_mk_fill(1, exchange="paradex"))
    assert tmp_ledger.count() == 2


# ── Query ────────────────────────────────────────────────────────────

def test_count(tmp_ledger):
    assert tmp_ledger.count() == 0
    tmp_ledger.append(_mk_fill(1))
    tmp_ledger.append(_mk_fill(2))
    assert tmp_ledger.count() == 2


def test_fills_in_window(tmp_ledger):
    base = datetime(2026, 4, 17, 12, 0, tzinfo=timezone.utc)
    tmp_ledger.append(_mk_fill(1, ts=base))
    tmp_ledger.append(_mk_fill(2, ts=base + timedelta(hours=1)))
    tmp_ledger.append(_mk_fill(3, ts=base + timedelta(hours=3)))
    results = tmp_ledger.fills_in_window(
        start=base + timedelta(minutes=30),
        end=base + timedelta(hours=2),
    )
    assert len(results) == 1
    assert results[0].fill_id == "fill-2"


def test_fills_by_exchange(tmp_ledger):
    tmp_ledger.append(_mk_fill(1, exchange="nado"))
    tmp_ledger.append(_mk_fill(2, exchange="paradex"))
    tmp_ledger.append(_mk_fill(3, exchange="nado"))
    nado = tmp_ledger.fills_by_exchange("nado")
    assert len(nado) == 2


def test_total_pnl_net(tmp_ledger):
    tmp_ledger.append(_mk_fill(1, close=False, fee=0.04))  # open, no pnl
    tmp_ledger.append(_mk_fill(2, close=True, pnl=1.00, fee=0.04, linked="fill-1"))  # close, gross +1
    # Total net = 0 (open pays fee, but gross pnl = 0) + (1.00 - 0.04) = 0.96 for close
    # BUT opening fee is also a cost → total net across all fills = (0 - 0.04) + (1.00 - 0.04) = 0.92
    assert tmp_ledger.total_pnl_net() == pytest.approx(0.92)


def test_total_pnl_net_filter_by_exchange(tmp_ledger):
    tmp_ledger.append(_mk_fill(1, exchange="nado", close=False, fee=0.04))
    tmp_ledger.append(_mk_fill(2, exchange="nado", close=True, pnl=2.0, fee=0.04, linked="fill-1"))
    tmp_ledger.append(_mk_fill(3, exchange="paradex", close=False, fee=-0.02))  # rebate
    assert tmp_ledger.total_pnl_net(exchange="nado") == pytest.approx(1.92)
    assert tmp_ledger.total_pnl_net(exchange="paradex") == pytest.approx(0.02)


def test_unreconciled_opens(tmp_ledger):
    """Opens without matching close fills."""
    tmp_ledger.append(_mk_fill(1, close=False))  # open 1
    tmp_ledger.append(_mk_fill(2, close=False))  # open 2
    tmp_ledger.append(_mk_fill(3, close=True, pnl=1.0, linked="fill-1"))  # closes 1
    opens = tmp_ledger.unreconciled_opens()
    assert len(opens) == 1
    assert opens[0].fill_id == "fill-2"


# ── Persistence ──────────────────────────────────────────────────────

def test_ledger_reload_from_disk(tmp_path):
    path = tmp_path / "persist.jsonl"
    l1 = Ledger(path)
    l1.append(_mk_fill(1))
    l1.append(_mk_fill(2))

    l2 = Ledger(path)  # new instance, same file
    assert l2.count() == 2


def test_ledger_append_fsync(tmp_ledger, monkeypatch):
    """Each append must fsync to survive crashes."""
    calls = []
    orig_fsync = __import__("os").fsync
    def tracking_fsync(fd):
        calls.append(fd)
        return orig_fsync(fd)
    monkeypatch.setattr("os.fsync", tracking_fsync)
    tmp_ledger.append(_mk_fill(1))
    assert len(calls) >= 1  # at least one fsync per append
