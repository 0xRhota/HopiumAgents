"""Daily PnL + adverse-selection reports for paper accounts.

Reads `logs/paper/{account}_ledger.jsonl` and prints a structured summary:
  - Total fills, by side, by venue
  - Realized PnL (close-side fills against weighted-avg entry)
  - Cumulative fees paid (positive = paid; negative = received rebate)
  - Cumulative adverse-selection cost (post-fill 30s mid drift)
  - Trade frequency stats (trades/day; vs config caps)
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class AccountReport:
    account: str
    fills: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    by_venue: Dict[str, int] = field(default_factory=dict)
    by_symbol: Dict[str, int] = field(default_factory=dict)
    cumulative_fees_usd: float = 0.0
    cumulative_adverse_bps: float = 0.0       # mean
    cumulative_adverse_count: int = 0
    realized_pnl_usd: float = 0.0
    open_position_count: int = 0
    open_position_notional_usd: float = 0.0
    total_volume_usd: float = 0.0
    maker_volume_usd: float = 0.0
    taker_volume_usd: float = 0.0
    volume_by_venue_usd: Dict[str, float] = field(default_factory=dict)

    def render(self) -> str:
        lines = [
            f"=== Paper account: {self.account} ===",
            f"Total fills: {self.fills} (maker={self.maker_fills}, taker={self.taker_fills})",
            f"By venue: {dict(self.by_venue)}",
            f"By symbol: {dict(sorted(self.by_symbol.items(), key=lambda kv: -kv[1])[:10])}",
            f"Total volume: ${self.total_volume_usd:,.2f}"
            f" (maker=${self.maker_volume_usd:,.2f}, taker=${self.taker_volume_usd:,.2f})",
            f"Volume by venue: " + ", ".join(
                f"{v}=${u:,.2f}" for v, u in sorted(
                    self.volume_by_venue_usd.items(), key=lambda kv: -kv[1])
            ),
            f"Realized PnL: ${self.realized_pnl_usd:+.2f}",
            f"Cumulative fees: ${self.cumulative_fees_usd:+.2f}"
            f" (negative = rebates received)",
        ]
        if self.cumulative_adverse_count > 0:
            lines.append(
                f"Adverse selection: avg {self.cumulative_adverse_bps:+.2f} bps"
                f" over {self.cumulative_adverse_count} maker fills"
                f" (positive = favorable; negative = picked off)"
            )
        else:
            lines.append("Adverse selection: no maker fills with T+30s data yet")
        lines.append(f"Open positions: {self.open_position_count}"
                     f" (notional ${self.open_position_notional_usd:.2f})")
        return "\n".join(lines)


def report_for_account(ledger_path: str | Path) -> AccountReport:
    """Generate a structured report from a paper ledger file."""
    p = Path(ledger_path)
    rep = AccountReport(account=p.stem.replace("_ledger", ""))
    if not p.exists():
        return rep

    # Track positions for realized PnL
    positions: Dict[tuple, dict] = {}  # (venue, symbol) -> {side, size, entry}
    adverse_drifts: List[float] = []

    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        f = json.loads(line)
        rep.fills += 1
        notional = float(f.get("price", 0)) * float(f.get("size", 0))
        rep.total_volume_usd += notional
        rep.volume_by_venue_usd[f["venue"]] = \
            rep.volume_by_venue_usd.get(f["venue"], 0.0) + notional
        if f.get("is_maker"):
            rep.maker_fills += 1
            rep.maker_volume_usd += notional
        else:
            rep.taker_fills += 1
            rep.taker_volume_usd += notional
        rep.by_venue[f["venue"]] = rep.by_venue.get(f["venue"], 0) + 1
        rep.by_symbol[f["symbol"]] = rep.by_symbol.get(f["symbol"], 0) + 1
        rep.cumulative_fees_usd += float(f.get("fee_paid_usd", 0))

        drift = f.get("adverse_drift_bps_t30")
        if drift is not None:
            adverse_drifts.append(float(drift))

        _apply_for_pnl(positions, f, rep)

    if adverse_drifts:
        rep.cumulative_adverse_bps = statistics.mean(adverse_drifts)
        rep.cumulative_adverse_count = len(adverse_drifts)

    rep.open_position_count = len(positions)
    rep.open_position_notional_usd = sum(
        p["size"] * p["entry"] for p in positions.values()
    )
    return rep


def _apply_for_pnl(positions: Dict, fill: dict, rep: AccountReport) -> None:
    key = (fill["venue"], fill["symbol"])
    side = fill["side"]
    size = float(fill["size"])
    price = float(fill["price"])
    sign = 1 if side == "BUY" else -1

    if key not in positions:
        positions[key] = {"side": side, "size": size, "entry": price}
        return

    existing = positions[key]
    existing_sign = 1 if existing["side"] == "BUY" else -1

    if side == existing["side"]:
        # Adding to position — weighted-average entry
        new_size = existing["size"] + size
        total = existing["entry"] * existing["size"] + price * size
        positions[key] = {
            "side": existing["side"], "size": new_size,
            "entry": total / new_size,
        }
    else:
        # Reducing or flipping
        if size < existing["size"]:
            # Partial close — realize PnL on `size` units
            pnl = (price - existing["entry"]) * size * existing_sign
            rep.realized_pnl_usd += pnl
            existing["size"] -= size
        elif abs(size - existing["size"]) < 1e-12:
            # Exact close — flat
            pnl = (price - existing["entry"]) * existing["size"] * existing_sign
            rep.realized_pnl_usd += pnl
            del positions[key]
        else:
            # Flip — realize all of old, open new
            pnl = (price - existing["entry"]) * existing["size"] * existing_sign
            rep.realized_pnl_usd += pnl
            positions[key] = {
                "side": side, "size": size - existing["size"], "entry": price,
            }


def report_all(ledger_dir: str | Path = "logs/paper") -> List[AccountReport]:
    base = Path(ledger_dir)
    if not base.exists():
        return []
    out: List[AccountReport] = []
    for p in sorted(base.glob("*_ledger.jsonl")):
        out.append(report_for_account(p))
    return out
