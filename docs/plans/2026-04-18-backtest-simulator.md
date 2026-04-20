# Backtest Simulator Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a perp-DEX backtest simulator that emits ledger-shaped Fill records under realistic fee + POST_ONLY rejection models, so backtest PnL is directly comparable to live ledger PnL.

**Architecture:** Event-driven loop processes 15m candles bar-by-bar. A `Portfolio` tracks multi-asset positions with leverage. Per-exchange `ExchangeSimulator` classes apply real maker/taker fee schedules and model POST_ONLY rejections (limit crosses book → reject, fall through to taker or skip). The runner emits `core.reconciliation.base.Fill` records to a sim-ledger file. A `compare.py` tool diffs sim_pnl vs live ledger_pnl to validate trustworthiness.

**Tech Stack:** Python 3.9+, pytest, pandas, numpy, asyncio. Reuses `core.reconciliation` Fill/Ledger dataclasses for output schema. Reuses `core.strategies.momentum.engine.MomentumEngine` for signal generation.

---

## Background — why this exists

The bot currently computes per-trade PnL as `(exit_price - entry_price) × size` (gross of fees, no slippage). This diverges from exchange equity by 7000% over 24h. We just built `core/reconciliation/` (Apr 17) to make LIVE PnL trustworthy. Now we need the BACKTEST equivalent so we can:
1. Verify a new strategy is actually +EV before deploying
2. Build user confidence ("can I fund $5k?") via measurable sim↔live agreement
3. Test alternative strategies (order book imbalance, funding farming, cross-sectional rank) without risking capital

Without this, every strategy decision is faith-based.

## Files that must exist when done

```
core/backtest/
├── __init__.py
├── portfolio.py        Multi-asset Portfolio with leverage and per-position state
├── exchange_sim.py     Per-exchange fee schedules + POST_ONLY rejection models
├── runner.py           Event-driven loop emitting ledger-shaped Fill records
├── walk_forward.py     Train/test rolling-window harness
├── grid_search.py      Parameter sweep + heatmap output
├── kelly.py            Kelly fraction from trade history
└── compare.py          sim_pnl vs ledger_pnl divergence checker

tests/backtest/
├── __init__.py
├── test_portfolio.py
├── test_exchange_sim.py
├── test_runner.py
├── test_walk_forward.py
├── test_kelly.py
└── test_compare.py

scripts/
├── run_backtest.py        CLI: backtest a strategy over a window
└── validate_strategy.py   CLI: compare sim PnL to live ledger PnL
```

## Verified inputs (from probes 2026-04-17 / 18)

**Per-exchange fee schedules — embed these as constants:**

| Exchange | Maker (signed) | Taker | POST_ONLY support | Notes |
|---|---|---|---|---|
| Paradex | -0.0050% (rebate, negative fee) | +0.0200% | Yes | 100% of last 100 fills were TAKER — rebate is sitting on the table |
| Nado | +0.0100% (1 bps) | +0.0350% (3.5 bps) | Yes | Most fills already maker (~57%) |
| Hibachi | +0.0000% (no maker fee — but ALO not exposed in our SDK yet) | +0.3500% (35 bps) | **No (SDK gap)** | Treat all closes as taker until SDK extension lands |

**Slippage model (conservative defaults):**
- Maker fill (POST_ONLY accepted): 0 ticks slippage
- Taker fill (market order or POST_ONLY fallback): 2 ticks slippage in the direction of trade

**POST_ONLY rejection rule:**
- BUY POST_ONLY rejects if `limit_price >= mid + half_spread` (would be a taker)
- SELL POST_ONLY rejects if `limit_price <= mid - half_spread`
- On rejection: skip the cycle (do NOT fall through to taker — that's a strategy choice the runner enforces)

## Hard constraints

- Output Fill records MUST match `core.reconciliation.base.Fill` schema exactly. Test this with `Fill(**sim_record_dict)` round-trip.
- All PnL math MUST account for `is_maker` flag (rebate vs fee).
- Multi-asset Portfolio MUST track positions per symbol independently.
- Leverage-aware: buying_power = equity × leverage. Position sizing decisions use buying_power, not equity.
- TDD: every task has a failing test BEFORE implementation. No exceptions.
- Datetimes: tz-aware UTC only. Naive datetimes are rejected (already enforced in Fill dataclass).
- Frequent commits: one commit per task minimum.

---

## Task 1: Scaffold core/backtest/ + first failing test

**Files:**
- Create: `core/backtest/__init__.py` (empty)
- Create: `tests/backtest/__init__.py` (empty)
- Create: `tests/backtest/test_portfolio.py`

**Step 1: Write the failing test**

```python
# tests/backtest/test_portfolio.py
import pytest
from core.backtest.portfolio import Portfolio


def test_portfolio_initializes_with_starting_equity():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    assert p.equity == 100.0
    assert p.leverage == 10.0
    assert p.buying_power == 1000.0
    assert p.positions == {}
```

**Step 2: Run to verify failure**

Run: `python3 -m pytest tests/backtest/test_portfolio.py -v`
Expected: `ModuleNotFoundError: No module named 'core.backtest.portfolio'`

**Step 3: Minimal implementation**

```python
# core/backtest/portfolio.py
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Portfolio:
    starting_equity: float
    leverage: float = 1.0
    equity: float = field(init=False)
    positions: Dict[str, dict] = field(default_factory=dict)

    def __post_init__(self):
        self.equity = self.starting_equity

    @property
    def buying_power(self) -> float:
        return self.equity * self.leverage
```

**Step 4: Verify pass**

Run: `python3 -m pytest tests/backtest/test_portfolio.py -v`
Expected: `1 passed`

**Step 5: Commit**

```bash
git add core/backtest/__init__.py core/backtest/portfolio.py tests/backtest/__init__.py tests/backtest/test_portfolio.py
git commit -m "feat(backtest): scaffold Portfolio with leverage-aware buying power"
```

---

## Task 2: Portfolio open_position / close_position with fee accounting

**Files:**
- Modify: `core/backtest/portfolio.py`
- Modify: `tests/backtest/test_portfolio.py`

**Step 1: Add failing tests**

```python
# Append to tests/backtest/test_portfolio.py
from datetime import datetime, timezone


def _ts():
    return datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)


def test_open_long_deducts_fee_and_records_position():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position(
        symbol="BTC-PERP", side="LONG", size=0.001, price=70000.0,
        fee=0.07, ts=_ts(), is_maker=False,
    )
    assert "BTC-PERP" in p.positions
    pos = p.positions["BTC-PERP"]
    assert pos["side"] == "LONG"
    assert pos["size"] == 0.001
    assert pos["entry_price"] == 70000.0
    # Fee deducted from equity (taker fee = positive)
    assert p.equity == pytest.approx(99.93)


def test_open_with_maker_rebate_increases_equity():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position(
        symbol="BTC-USD-PERP", side="LONG", size=0.001, price=70000.0,
        fee=-0.0035, ts=_ts(), is_maker=True,  # Paradex maker rebate
    )
    assert p.equity == pytest.approx(100.0035)


def test_close_long_realizes_pnl_and_deducts_fee():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("BTC-PERP", "LONG", 0.001, 70000.0, fee=0.07, ts=_ts(), is_maker=False)
    p.close_position("BTC-PERP", price=71000.0, fee=0.071, ts=_ts(), is_maker=False)
    # Realized = (71000 - 70000) * 0.001 = 1.00
    # Fees on entry+exit = 0.141
    # Net change = 1.00 - 0.141 = 0.859
    assert "BTC-PERP" not in p.positions
    assert p.equity == pytest.approx(100.859)


def test_close_short_realizes_inverted_pnl():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("ETH-PERP", "SHORT", 0.04, 2300.0, fee=0.032, ts=_ts(), is_maker=False)
    p.close_position("ETH-PERP", price=2280.0, fee=0.032, ts=_ts(), is_maker=False)
    # Realized = (2300 - 2280) * 0.04 = 0.80
    # Fees = 0.064
    # Net = 0.736
    assert p.equity == pytest.approx(100.736)


def test_close_unknown_symbol_raises():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    with pytest.raises(KeyError):
        p.close_position("BTC-PERP", price=70000.0, fee=0.07, ts=_ts(), is_maker=False)


def test_open_when_already_in_position_raises():
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("BTC-PERP", "LONG", 0.001, 70000.0, fee=0.07, ts=_ts(), is_maker=False)
    with pytest.raises(ValueError):
        p.open_position("BTC-PERP", "LONG", 0.001, 70000.0, fee=0.07, ts=_ts(), is_maker=False)
```

**Step 2: Verify failure**

Run: `python3 -m pytest tests/backtest/test_portfolio.py -v`
Expected: 6 failures (`open_position` not defined etc.)

**Step 3: Implement**

```python
# Append to core/backtest/portfolio.py
from datetime import datetime
from typing import Literal

Side = Literal["LONG", "SHORT"]


@dataclass
class Portfolio:
    # ... existing fields ...

    def open_position(self, symbol: str, side: Side, size: float, price: float,
                      fee: float, ts: datetime, is_maker: bool) -> None:
        if symbol in self.positions:
            raise ValueError(f"already in position on {symbol}")
        if side not in ("LONG", "SHORT"):
            raise ValueError(f"invalid side: {side!r}")
        self.positions[symbol] = {
            "side": side, "size": size, "entry_price": price,
            "entry_ts": ts, "entry_fee": fee, "is_maker_entry": is_maker,
        }
        self.equity -= fee  # fee positive = paid; negative = rebate adds to equity

    def close_position(self, symbol: str, price: float, fee: float,
                       ts: datetime, is_maker: bool) -> float:
        pos = self.positions.pop(symbol)  # raises KeyError if missing
        if pos["side"] == "LONG":
            realized = (price - pos["entry_price"]) * pos["size"]
        else:
            realized = (pos["entry_price"] - price) * pos["size"]
        self.equity += realized - fee
        return realized
```

**Step 4: Verify pass**

Run: `python3 -m pytest tests/backtest/test_portfolio.py -v`
Expected: `7 passed`

**Step 5: Commit**

```bash
git add core/backtest/portfolio.py tests/backtest/test_portfolio.py
git commit -m "feat(backtest): Portfolio open/close with fee accounting and PnL realization"
```

---

## Task 3: ExchangeSimulator — fee schedules + POST_ONLY rejection (Nado)

**Files:**
- Create: `core/backtest/exchange_sim.py`
- Create: `tests/backtest/test_exchange_sim.py`

**Step 1: Failing tests**

```python
# tests/backtest/test_exchange_sim.py
import pytest
from core.backtest.exchange_sim import simulate_order, ExchangeSpec, NADO, HIBACHI, PARADEX


def test_nado_taker_market_buy_fee():
    """3.5 bps × $100 notional = $0.035 paid."""
    fill = simulate_order(NADO, side="BUY", size=100.0, price=1.0, mid=1.0,
                          half_spread=0.0001, post_only=False)
    assert fill is not None
    assert fill["is_maker"] is False
    assert fill["fee"] == pytest.approx(0.035, abs=1e-6)
    assert fill["fill_price"] > 1.0  # taker pays slippage


def test_nado_post_only_accepts_when_below_bid():
    """BUY POST_ONLY at price below current bid — should rest as maker."""
    fill = simulate_order(NADO, side="BUY", size=100.0, price=0.99, mid=1.0,
                          half_spread=0.005, post_only=True)
    assert fill is not None
    assert fill["is_maker"] is True
    assert fill["fee"] == pytest.approx(0.0099, abs=1e-6)  # 1 bps × $99 notional
    assert fill["fill_price"] == 0.99  # no taker slippage on maker


def test_nado_post_only_rejects_when_crosses_book():
    """BUY POST_ONLY at price above current ask — rejected."""
    fill = simulate_order(NADO, side="BUY", size=100.0, price=1.10, mid=1.0,
                          half_spread=0.005, post_only=True)
    assert fill is None  # rejected, no fill


def test_paradex_maker_rebate_is_negative_fee():
    fill = simulate_order(PARADEX, side="SELL", size=0.001, price=70000.0,
                          mid=70010.0, half_spread=2.0, post_only=True)
    assert fill is not None
    assert fill["is_maker"] is True
    assert fill["fee"] < 0  # rebate
    assert fill["fee"] == pytest.approx(-0.0035, abs=1e-6)  # -0.5 bps × $70


def test_hibachi_taker_high_fee():
    """35 bps taker on Hibachi."""
    fill = simulate_order(HIBACHI, side="SELL", size=0.001, price=70000.0,
                          mid=70000.0, half_spread=1.0, post_only=False)
    assert fill is not None
    assert fill["is_maker"] is False
    assert fill["fee"] == pytest.approx(0.245, abs=1e-3)  # 0.35% × $70


def test_hibachi_rejects_post_only_because_sdk_does_not_support_it():
    """Until SDK supports ALO, POST_ONLY is unavailable on Hibachi."""
    fill = simulate_order(HIBACHI, side="BUY", size=0.001, price=69990.0,
                          mid=70000.0, half_spread=1.0, post_only=True)
    assert fill is None
```

**Step 2: Verify failure**

Run: `python3 -m pytest tests/backtest/test_exchange_sim.py -v`
Expected: import error

**Step 3: Implement**

```python
# core/backtest/exchange_sim.py
"""Per-exchange fee + POST_ONLY rejection simulation.

Verified live 2026-04-17/18 against each exchange's API.
Update fee schedules here when exchanges change them.
"""
from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True)
class ExchangeSpec:
    name: str
    maker_fee_bps: float            # signed: negative = rebate
    taker_fee_bps: float            # always positive
    supports_post_only: bool
    taker_slippage_ticks: int = 2
    tick_size: float = 0.0001       # default; runner overrides per-symbol


NADO    = ExchangeSpec(name="nado",    maker_fee_bps=1.0,  taker_fee_bps=3.5,  supports_post_only=True)
PARADEX = ExchangeSpec(name="paradex", maker_fee_bps=-0.5, taker_fee_bps=2.0,  supports_post_only=True)
HIBACHI = ExchangeSpec(name="hibachi", maker_fee_bps=0.0,  taker_fee_bps=35.0, supports_post_only=False)


def simulate_order(spec: ExchangeSpec, side: Literal["BUY", "SELL"],
                   size: float, price: float, mid: float, half_spread: float,
                   post_only: bool) -> Optional[dict]:
    """Returns a fill dict or None (rejection)."""
    if post_only:
        if not spec.supports_post_only:
            return None
        # POST_ONLY: limit must NOT cross the book
        if side == "BUY" and price >= mid + half_spread:
            return None
        if side == "SELL" and price <= mid - half_spread:
            return None
        # Maker fill
        notional = abs(size * price)
        fee = notional * (spec.maker_fee_bps / 10_000.0)
        return {"is_maker": True, "fill_price": price, "fee": fee}

    # Taker path
    slip_dir = 1 if side == "BUY" else -1
    fill_price = mid + slip_dir * (half_spread + spec.taker_slippage_ticks * spec.tick_size)
    notional = abs(size * fill_price)
    fee = notional * (spec.taker_fee_bps / 10_000.0)
    return {"is_maker": False, "fill_price": fill_price, "fee": fee}
```

**Step 4: Verify pass**

Run: `python3 -m pytest tests/backtest/test_exchange_sim.py -v`
Expected: `6 passed`

**Step 5: Commit**

```bash
git add core/backtest/exchange_sim.py tests/backtest/test_exchange_sim.py
git commit -m "feat(backtest): per-exchange fee schedules + POST_ONLY rejection model"
```

---

## Task 4: Runner — event-driven loop emitting ledger-shaped Fill records

**Files:**
- Create: `core/backtest/runner.py`
- Create: `tests/backtest/test_runner.py`

**Step 1: Failing test**

```python
# tests/backtest/test_runner.py
import pytest
import pandas as pd
from datetime import datetime, timezone
from core.backtest.runner import run_backtest
from core.backtest.exchange_sim import NADO
from core.reconciliation.base import Fill


class _StubStrategy:
    """Buy on bar 2, sell on bar 5."""
    def on_bar(self, ts, bar, portfolio):
        if ts.minute == 30:  # bar 2
            return [{"action": "OPEN", "symbol": "LIT-PERP", "side": "LONG",
                     "size": 100.0, "limit_price": bar["close"], "post_only": True}]
        if ts.minute == 45:  # bar 5
            return [{"action": "CLOSE", "symbol": "LIT-PERP",
                     "limit_price": bar["close"], "post_only": False}]
        return []


def _bars():
    idx = pd.date_range("2026-04-18 12:15", periods=8, freq="5min", tz="UTC")
    closes = [1.00, 1.01, 1.02, 1.03, 1.04, 1.05, 1.06, 1.07]
    return pd.DataFrame({"close": closes, "high": closes, "low": closes,
                         "open": closes, "volume": [100]*8}, index=idx)


def test_runner_emits_fill_records_in_ledger_schema():
    fills = run_backtest(strategy=_StubStrategy(), bars=_bars(), exchange=NADO,
                         starting_equity=100.0, leverage=10.0)
    assert all(isinstance(f, Fill) for f in fills)
    assert len(fills) == 2
    assert fills[0].opens_or_closes == "OPEN"
    assert fills[1].opens_or_closes == "CLOSE"
    assert fills[0].exchange == "nado"


def test_runner_skips_when_post_only_rejects():
    """Strategy tries to BUY POST_ONLY at a price above mid — rejected."""
    class _BadEntry:
        def on_bar(self, ts, bar, portfolio):
            if ts.minute == 30:
                return [{"action": "OPEN", "symbol": "X", "side": "LONG",
                         "size": 100.0, "limit_price": bar["close"] * 2,  # crosses book
                         "post_only": True}]
            return []
    fills = run_backtest(strategy=_BadEntry(), bars=_bars(), exchange=NADO,
                         starting_equity=100.0, leverage=10.0)
    assert len(fills) == 0


def test_runner_realized_pnl_matches_portfolio_equity_delta():
    fills = run_backtest(strategy=_StubStrategy(), bars=_bars(), exchange=NADO,
                         starting_equity=100.0, leverage=10.0)
    close_fill = next(f for f in fills if f.opens_or_closes == "CLOSE")
    # Entry at 1.01 (post-only), exit at 1.04 taker — but check realized field is populated
    assert close_fill.realized_pnl_usd is not None
    assert close_fill.realized_pnl_usd > 0
```

**Step 2: Verify failure**

Run: `python3 -m pytest tests/backtest/test_runner.py -v`
Expected: import error

**Step 3: Implement**

```python
# core/backtest/runner.py
"""Event-driven backtest loop.

Strategy interface: an object with method
    on_bar(ts, bar, portfolio) -> List[dict]
where each dict is {"action": "OPEN"|"CLOSE", "symbol": str, ...}.
"""
from datetime import datetime
from typing import List
import pandas as pd

from core.backtest.exchange_sim import ExchangeSpec, simulate_order
from core.backtest.portfolio import Portfolio
from core.reconciliation.base import Fill


def run_backtest(strategy, bars: pd.DataFrame, exchange: ExchangeSpec,
                 starting_equity: float, leverage: float = 1.0,
                 half_spread_bps: float = 5.0) -> List[Fill]:
    portfolio = Portfolio(starting_equity=starting_equity, leverage=leverage)
    fills: List[Fill] = []
    fill_counter = 0

    for ts, row in bars.iterrows():
        actions = strategy.on_bar(ts, row, portfolio)
        for act in actions:
            symbol = act["symbol"]
            mid = float(row["close"])
            half_spread = mid * (half_spread_bps / 10_000.0)
            limit_price = float(act["limit_price"])
            post_only = bool(act.get("post_only", False))

            if act["action"] == "OPEN":
                side_order = "BUY" if act["side"] == "LONG" else "SELL"
                size = float(act["size"]) / mid  # notional → base units
                sim = simulate_order(exchange, side=side_order, size=size,
                                     price=limit_price, mid=mid,
                                     half_spread=half_spread, post_only=post_only)
                if sim is None:
                    continue
                portfolio.open_position(symbol=symbol, side=act["side"],
                                        size=size, price=sim["fill_price"],
                                        fee=sim["fee"], ts=ts,
                                        is_maker=sim["is_maker"])
                fill_counter += 1
                fills.append(Fill(
                    exchange=exchange.name, symbol=symbol,
                    fill_id=f"sim-{fill_counter}", order_id=f"sim-{fill_counter}",
                    ts=ts, side=side_order, size=size,
                    price=sim["fill_price"], fee=sim["fee"],
                    is_maker=sim["is_maker"],
                    realized_pnl_usd=None, opens_or_closes="OPEN",
                ))
            elif act["action"] == "CLOSE":
                if symbol not in portfolio.positions:
                    continue
                pos = portfolio.positions[symbol]
                side_order = "SELL" if pos["side"] == "LONG" else "BUY"
                sim = simulate_order(exchange, side=side_order, size=pos["size"],
                                     price=limit_price, mid=mid,
                                     half_spread=half_spread, post_only=post_only)
                if sim is None:
                    continue
                realized = portfolio.close_position(
                    symbol=symbol, price=sim["fill_price"], fee=sim["fee"],
                    ts=ts, is_maker=sim["is_maker"],
                )
                fill_counter += 1
                fills.append(Fill(
                    exchange=exchange.name, symbol=symbol,
                    fill_id=f"sim-{fill_counter}", order_id=f"sim-{fill_counter}",
                    ts=ts, side=side_order, size=pos["size"],
                    price=sim["fill_price"], fee=sim["fee"],
                    is_maker=sim["is_maker"],
                    realized_pnl_usd=realized, opens_or_closes="CLOSE",
                ))
    return fills
```

**Step 4: Verify pass**

Run: `python3 -m pytest tests/backtest/test_runner.py -v`
Expected: `3 passed`

**Step 5: Commit**

```bash
git add core/backtest/runner.py tests/backtest/test_runner.py
git commit -m "feat(backtest): event-driven runner emitting ledger-shaped Fill records"
```

---

## Task 5: Adapt MomentumEngine into a backtest-compatible Strategy

**Files:**
- Create: `core/backtest/momentum_strategy.py`
- Create: `tests/backtest/test_momentum_strategy.py`

**Step 1: Failing test**

```python
# tests/backtest/test_momentum_strategy.py
import pandas as pd
import pytest
from datetime import datetime, timezone
from core.backtest.momentum_strategy import BacktestMomentumStrategy
from core.backtest.portfolio import Portfolio


def _bars(closes):
    idx = pd.date_range("2026-04-18", periods=len(closes), freq="15min", tz="UTC")
    return pd.DataFrame({"close": closes, "high": closes, "low": closes,
                         "open": closes, "volume": [1000]*len(closes)}, index=idx)


def test_strategy_emits_no_action_below_score_threshold():
    s = BacktestMomentumStrategy(symbol="LIT-PERP", score_min=2.5)
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    # Flat candles → low signals → no action
    bars = _bars([1.0] * 50)
    actions = s.on_bar(bars.index[-1], bars.iloc[-1], p, history=bars)
    assert actions == []


def test_strategy_emits_open_when_score_clears_threshold():
    """Strong uptrend → high RSI/MACD/PA → score crosses → BUY."""
    s = BacktestMomentumStrategy(symbol="LIT-PERP", score_min=2.5)
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    bars = _bars([1.0 + 0.01 * i for i in range(50)])
    actions = s.on_bar(bars.index[-1], bars.iloc[-1], p, history=bars)
    assert len(actions) == 1
    assert actions[0]["action"] == "OPEN"
    assert actions[0]["side"] == "LONG"


def test_strategy_emits_close_when_tp_hit():
    s = BacktestMomentumStrategy(symbol="LIT-PERP", tp_bps=80, sl_bps=40)
    p = Portfolio(starting_equity=100.0, leverage=10.0)
    p.open_position("LIT-PERP", "LONG", 100.0, 1.0,
                    fee=0.01, ts=datetime(2026, 4, 18, tzinfo=timezone.utc),
                    is_maker=True)
    bars = _bars([1.0, 1.005, 1.009])  # +90bps
    actions = s.on_bar(bars.index[-1], bars.iloc[-1], p, history=bars)
    assert len(actions) == 1
    assert actions[0]["action"] == "CLOSE"
```

**Step 2: Verify failure**

Run: `python3 -m pytest tests/backtest/test_momentum_strategy.py -v`
Expected: import error

**Step 3: Implement (skeleton — full signal calc reuses existing engine)**

```python
# core/backtest/momentum_strategy.py
"""Momentum strategy adapter for backtest.

Wraps core.strategies.momentum.engine.MomentumEngine to implement the
runner's strategy interface (on_bar method). Same scoring as live so
backtest results are directly comparable.
"""
from typing import List
import pandas as pd

from core.strategies.momentum.engine import MomentumEngine, MomentumConfig


class BacktestMomentumStrategy:
    def __init__(self, symbol: str, score_min: float = 2.5,
                 tp_bps: float = 80.0, sl_bps: float = 40.0,
                 size_pct: float = 20.0):
        self.symbol = symbol
        self.cfg = MomentumConfig(
            score_min=score_min, tp_bps=tp_bps, sl_bps=sl_bps,
            size_pct=size_pct,
        )
        self.engine = MomentumEngine(self.cfg)

    def on_bar(self, ts, bar, portfolio, history: pd.DataFrame = None) -> List[dict]:
        # If in position: check exit
        if self.symbol in portfolio.positions:
            pos = portfolio.positions[self.symbol]
            entry = pos["entry_price"]
            current = float(bar["close"])
            if pos["side"] == "LONG":
                bps_move = (current - entry) / entry * 10_000
            else:
                bps_move = (entry - current) / entry * 10_000
            if bps_move >= self.cfg.tp_bps or bps_move <= -self.cfg.sl_bps:
                return [{"action": "CLOSE", "symbol": self.symbol,
                         "limit_price": current, "post_only": False}]
            return []

        # Not in position: check entry signal
        if history is None or len(history) < 50:
            return []
        trend = self.engine.score(history)
        if trend["score"] < self.cfg.score_min:
            return []
        side = "LONG" if trend["direction"] == "BUY" else "SHORT"
        size_usd = portfolio.buying_power * (self.cfg.size_pct / 100.0)
        return [{"action": "OPEN", "symbol": self.symbol, "side": side,
                 "size": size_usd, "limit_price": float(bar["close"]),
                 "post_only": True}]
```

**Step 4: Verify pass**

Run: `python3 -m pytest tests/backtest/test_momentum_strategy.py -v`
Expected: `3 passed`

If MomentumEngine.score() interface doesn't match — adapt: read core/strategies/momentum/engine.py:1-100 and align.

**Step 5: Commit**

```bash
git add core/backtest/momentum_strategy.py tests/backtest/test_momentum_strategy.py
git commit -m "feat(backtest): MomentumEngine adapter implementing runner Strategy protocol"
```

---

## Task 6: CLI runner — `scripts/run_backtest.py`

**Files:**
- Create: `scripts/run_backtest.py`
- Create: `tests/backtest/test_cli_smoke.py`

**Step 1: Smoke test**

```python
# tests/backtest/test_cli_smoke.py
import subprocess
import sys
from pathlib import Path


def test_cli_runs_and_outputs_summary(tmp_path):
    """Smoke test: CLI exits 0 and prints a summary line."""
    script = Path(__file__).parent.parent.parent / "scripts" / "run_backtest.py"
    result = subprocess.run(
        [sys.executable, str(script), "--symbol", "BTC-PERP", "--exchange", "nado",
         "--days", "1", "--score-min", "2.5"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "NET PnL" in result.stdout
```

**Step 2: Verify failure**

Run: `python3 -m pytest tests/backtest/test_cli_smoke.py -v`
Expected: FileNotFoundError on the script

**Step 3: Implement**

```python
# scripts/run_backtest.py
"""Run a backtest of the momentum strategy on historical Binance klines.

Usage:
    python3 scripts/run_backtest.py --symbol BTC-PERP --exchange nado --days 30
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import requests

from core.backtest.exchange_sim import NADO, HIBACHI, PARADEX
from core.backtest.momentum_strategy import BacktestMomentumStrategy
from core.backtest.runner import run_backtest


_EX_MAP = {"nado": NADO, "hibachi": HIBACHI, "paradex": PARADEX}


def fetch_binance_klines(symbol: str, days: int, interval: str = "15m") -> pd.DataFrame:
    binance_sym = symbol.replace("-PERP", "USDT").replace("/USDT-P", "USDT")
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = end - days * 24 * 60 * 60 * 1000
    resp = requests.get("https://api.binance.com/api/v3/klines", params={
        "symbol": binance_sym, "interval": interval,
        "startTime": start, "endTime": end, "limit": 1000,
    }, timeout=10)
    data = resp.json()
    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "tb_base", "tb_quote", "ignore",
    ])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["open", "high", "low", "close", "volume"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--exchange", required=True, choices=list(_EX_MAP))
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--score-min", type=float, default=2.5)
    ap.add_argument("--starting-equity", type=float, default=100.0)
    args = ap.parse_args()

    bars = fetch_binance_klines(args.symbol, args.days)
    if len(bars) < 50:
        print(f"Not enough bars ({len(bars)})")
        sys.exit(1)

    strategy = BacktestMomentumStrategy(symbol=args.symbol, score_min=args.score_min)

    fills = run_backtest(
        strategy=strategy, bars=bars, exchange=_EX_MAP[args.exchange],
        starting_equity=args.starting_equity, leverage=10.0,
    )

    realized = sum(f.realized_pnl_usd or 0 for f in fills)
    fees = sum(f.fee for f in fills)
    net = realized - fees
    closes = [f for f in fills if f.opens_or_closes == "CLOSE"]
    wins = sum(1 for f in closes if (f.realized_pnl_usd or 0) > 0)
    wr = wins / len(closes) * 100 if closes else 0

    print(f"\n=== Backtest: {args.symbol} on {args.exchange} ({args.days}d) ===")
    print(f"Trades:    {len(closes)}")
    print(f"Win rate:  {wr:.0f}%")
    print(f"Realized:  ${realized:+.2f}")
    print(f"Fees:      ${fees:+.2f}")
    print(f"NET PnL:   ${net:+.2f}")


if __name__ == "__main__":
    main()
```

**Step 4: Verify pass**

Run: `python3 -m pytest tests/backtest/test_cli_smoke.py -v`
Expected: `1 passed` (network call so allow up to 60s)

**Step 5: Commit**

```bash
git add scripts/run_backtest.py tests/backtest/test_cli_smoke.py
git commit -m "feat(backtest): CLI to run momentum strategy backtest with real Binance candles"
```

---

## Task 7: Walk-forward harness

**Files:**
- Create: `core/backtest/walk_forward.py`
- Create: `tests/backtest/test_walk_forward.py`

**Step 1: Failing tests**

```python
# tests/backtest/test_walk_forward.py
import pandas as pd
import pytest
from core.backtest.walk_forward import walk_forward, WindowResult


def _bars(n=2000):
    closes = [1.0 + 0.001 * (i % 100) for i in range(n)]
    idx = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"close": closes, "high": closes, "low": closes,
                         "open": closes, "volume": [100]*n}, index=idx)


def test_walk_forward_yields_one_result_per_test_window():
    """50 bars/window × 5-bar train + 5-bar test = N pairs."""
    bars = _bars(60)
    results = list(walk_forward(
        bars=bars, train_bars=20, test_bars=10,
        param_grid=[{"score_min": 2.5}, {"score_min": 3.0}],
        runner=lambda bars, params: 1.5 if params["score_min"] == 2.5 else 0.5,
    ))
    # bars=60, train=20, test=10 → windows: (0..20, 20..30), (10..30, 30..40), ...
    assert len(results) >= 3
    for r in results:
        assert isinstance(r, WindowResult)
        assert r.best_params is not None
        assert r.train_score is not None
        assert r.test_score is not None


def test_walk_forward_picks_best_param_from_train():
    bars = _bars(60)
    results = list(walk_forward(
        bars=bars, train_bars=20, test_bars=10,
        param_grid=[{"x": 1}, {"x": 2}, {"x": 3}],
        runner=lambda bars, params: params["x"] * 1.0,
    ))
    # x=3 always wins on train
    for r in results:
        assert r.best_params["x"] == 3
```

**Step 2: Verify failure**

Run: `python3 -m pytest tests/backtest/test_walk_forward.py -v`
Expected: import error

**Step 3: Implement**

```python
# core/backtest/walk_forward.py
"""Walk-forward analysis: rolling train/test windows for anti-overfitting.

Usage:
    for result in walk_forward(bars, train_bars=2880, test_bars=672,  # 30d / 7d at 15m
                                param_grid=[...], runner=fn):
        print(result.best_params, result.test_score)
"""
from dataclasses import dataclass
from typing import Callable, Dict, Iterator, List
import pandas as pd


@dataclass
class WindowResult:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_end: pd.Timestamp
    best_params: dict
    train_score: float
    test_score: float


def walk_forward(bars: pd.DataFrame, train_bars: int, test_bars: int,
                 param_grid: List[dict],
                 runner: Callable[[pd.DataFrame, dict], float]) -> Iterator[WindowResult]:
    """Slide train/test window forward by test_bars at each step.

    `runner(bars, params) -> float` is called per (window, params) and must
    return a scalar score (e.g. Sharpe or net PnL). Highest train score
    selects the best_params; that param set is then evaluated on test.
    """
    i = 0
    while i + train_bars + test_bars <= len(bars):
        train = bars.iloc[i : i + train_bars]
        test = bars.iloc[i + train_bars : i + train_bars + test_bars]
        best_params = None
        best_score = float("-inf")
        for params in param_grid:
            s = runner(train, params)
            if s > best_score:
                best_score = s
                best_params = params
        test_score = runner(test, best_params)
        yield WindowResult(
            train_start=train.index[0], train_end=train.index[-1],
            test_end=test.index[-1], best_params=best_params,
            train_score=best_score, test_score=test_score,
        )
        i += test_bars
```

**Step 4: Verify pass**

Run: `python3 -m pytest tests/backtest/test_walk_forward.py -v`
Expected: `2 passed`

**Step 5: Commit**

```bash
git add core/backtest/walk_forward.py tests/backtest/test_walk_forward.py
git commit -m "feat(backtest): walk-forward harness for out-of-sample parameter validation"
```

---

## Task 8: Sim-vs-ledger compare — THE TRUST GATE

**Files:**
- Create: `core/backtest/compare.py`
- Create: `scripts/validate_strategy.py`
- Create: `tests/backtest/test_compare.py`

**Step 1: Failing test**

```python
# tests/backtest/test_compare.py
import pytest
from datetime import datetime, timezone
from core.backtest.compare import compare_pnl, ComparisonResult
from core.reconciliation.base import Fill


def _fill(realized=None, fee=0.05, opens="OPEN"):
    return Fill(
        exchange="nado", symbol="LIT-PERP", fill_id="x", order_id="x",
        ts=datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc),
        side="BUY", size=100.0, price=1.0, fee=fee, is_maker=True,
        realized_pnl_usd=realized, opens_or_closes=opens,
    )


def test_compare_within_tolerance_passes():
    sim = [_fill(realized=1.0, opens="CLOSE")]
    live = [_fill(realized=1.05, opens="CLOSE")]
    r = compare_pnl(sim, live, tolerance_usd=1.0, tolerance_pct=0.05)
    assert isinstance(r, ComparisonResult)
    assert r.passed is True


def test_compare_beyond_tolerance_fails():
    sim = [_fill(realized=10.0, opens="CLOSE")]
    live = [_fill(realized=2.0, opens="CLOSE")]
    r = compare_pnl(sim, live, tolerance_usd=1.0, tolerance_pct=0.05)
    assert r.passed is False
    assert r.divergence_usd == pytest.approx(8.0)


def test_compare_reports_per_field_breakdown():
    sim = [_fill(realized=1.0, fee=0.05, opens="CLOSE")]
    live = [_fill(realized=1.0, fee=0.10, opens="CLOSE")]
    r = compare_pnl(sim, live, tolerance_usd=0.10, tolerance_pct=0.10)
    assert r.sim_fees == pytest.approx(0.05)
    assert r.live_fees == pytest.approx(0.10)
    assert "fees" in r.notes.lower() or "fee" in r.notes.lower()
```

**Step 2: Verify failure**

Run: `python3 -m pytest tests/backtest/test_compare.py -v`
Expected: import error

**Step 3: Implement**

```python
# core/backtest/compare.py
"""Sim vs Live PnL divergence checker — the trust gate.

A backtest strategy is only trustworthy once compare_pnl(sim, live)
passes for the same time window with the same strategy params.
"""
from dataclasses import dataclass
from typing import List
from core.reconciliation.base import Fill


@dataclass
class ComparisonResult:
    sim_realized: float
    live_realized: float
    sim_fees: float
    live_fees: float
    sim_net: float
    live_net: float
    divergence_usd: float
    divergence_pct: float
    passed: bool
    notes: str


def compare_pnl(sim: List[Fill], live: List[Fill],
                tolerance_usd: float = 1.0,
                tolerance_pct: float = 0.05) -> ComparisonResult:
    sim_realized = sum(f.realized_pnl_usd or 0 for f in sim)
    sim_fees = sum(f.fee for f in sim)
    sim_net = sim_realized - sim_fees

    live_realized = sum(f.realized_pnl_usd or 0 for f in live)
    live_fees = sum(f.fee for f in live)
    live_net = live_realized - live_fees

    div_usd = abs(sim_net - live_net)
    div_pct = div_usd / max(1e-9, abs(live_net))

    notes = []
    if abs(sim_realized - live_realized) > tolerance_usd:
        notes.append(f"realized differs: sim={sim_realized:+.2f} live={live_realized:+.2f}")
    if abs(sim_fees - live_fees) > tolerance_usd:
        notes.append(f"fees differ: sim={sim_fees:+.4f} live={live_fees:+.4f}")
    if len(sim) != len(live):
        notes.append(f"trade count differs: sim={len(sim)} live={len(live)}")

    passed = div_usd <= tolerance_usd or div_pct <= tolerance_pct

    return ComparisonResult(
        sim_realized=sim_realized, live_realized=live_realized,
        sim_fees=sim_fees, live_fees=live_fees,
        sim_net=sim_net, live_net=live_net,
        divergence_usd=div_usd, divergence_pct=div_pct,
        passed=passed, notes="; ".join(notes) or "ok",
    )
```

**Step 4: Verify pass**

Run: `python3 -m pytest tests/backtest/test_compare.py -v`
Expected: `3 passed`

**Step 5: CLI for the trust gate**

```python
# scripts/validate_strategy.py
"""Compare backtest sim PnL to live ledger PnL for a window.

Usage:
    python3.11 scripts/validate_strategy.py --exchange nado --symbol LIT-PERP --days 7
"""
import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from core.backtest.compare import compare_pnl
from core.backtest.exchange_sim import NADO, HIBACHI, PARADEX
from core.backtest.momentum_strategy import BacktestMomentumStrategy
from core.backtest.runner import run_backtest
from core.reconciliation import build_reconciler
from scripts.run_backtest import fetch_binance_klines


_EX_MAP = {"nado": NADO, "hibachi": HIBACHI, "paradex": PARADEX}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", required=True, choices=list(_EX_MAP))
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--tolerance-usd", type=float, default=1.0)
    ap.add_argument("--tolerance-pct", type=float, default=0.05)
    args = ap.parse_args()

    # Sim
    bars = fetch_binance_klines(args.symbol, args.days)
    strategy = BacktestMomentumStrategy(symbol=args.symbol)
    sim_fills = run_backtest(strategy=strategy, bars=bars,
                             exchange=_EX_MAP[args.exchange],
                             starting_equity=100.0, leverage=10.0)

    # Live (from reconciler — last `days` window)
    rec = build_reconciler(args.exchange)
    snap = await rec.snapshot()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    live_fills = [f for f in snap.new_fills
                  if f.symbol == args.symbol and f.ts >= cutoff]

    r = compare_pnl(sim_fills, live_fills,
                    tolerance_usd=args.tolerance_usd,
                    tolerance_pct=args.tolerance_pct)

    print(f"\n=== {args.symbol} on {args.exchange} — last {args.days}d ===")
    print(f"sim:  realized=${r.sim_realized:+.2f}  fees=${r.sim_fees:+.2f}  NET=${r.sim_net:+.2f}  ({len(sim_fills)} fills)")
    print(f"live: realized=${r.live_realized:+.2f}  fees=${r.live_fees:+.2f}  NET=${r.live_net:+.2f}  ({len(live_fills)} fills)")
    print(f"divergence: ${r.divergence_usd:.2f} ({r.divergence_pct*100:.1f}%)")
    print(f"PASSED: {r.passed}")
    print(f"notes: {r.notes}")
    sys.exit(0 if r.passed else 1)


if __name__ == "__main__":
    asyncio.run(main())
```

**Step 6: Commit**

```bash
git add core/backtest/compare.py scripts/validate_strategy.py tests/backtest/test_compare.py
git commit -m "feat(backtest): sim-vs-live PnL divergence checker (the trust gate)"
```

---

## Task 9: Kelly fraction + sizing variant

**Files:**
- Create: `core/backtest/kelly.py`
- Create: `tests/backtest/test_kelly.py`

**Step 1: Failing tests**

```python
# tests/backtest/test_kelly.py
import pytest
from datetime import datetime, timezone
from core.backtest.kelly import kelly_fraction
from core.reconciliation.base import Fill


def _close(realized, fee=0.0):
    return Fill(
        exchange="nado", symbol="X", fill_id="x", order_id="x",
        ts=datetime(2026, 4, 18, tzinfo=timezone.utc),
        side="SELL", size=1, price=1.0, fee=fee, is_maker=True,
        realized_pnl_usd=realized, opens_or_closes="CLOSE",
    )


def test_kelly_zero_when_no_wins():
    assert kelly_fraction([_close(-1.0), _close(-2.0)]) == 0.0


def test_kelly_zero_when_no_losses():
    """All wins → no risk frame; degenerate, return 0.0."""
    assert kelly_fraction([_close(1.0), _close(2.0)]) == 0.0


def test_kelly_simple_case():
    # 60% WR, avg_win=$2, avg_loss=$1 → b=2, p=0.6 → kelly=(0.6*2 - 0.4)/2 = 0.4
    fills = [_close(2.0)] * 6 + [_close(-1.0)] * 4
    assert kelly_fraction(fills) == pytest.approx(0.4, abs=1e-3)


def test_kelly_clipped_to_zero_one():
    """High-edge bet → kelly capped at 1.0."""
    fills = [_close(10.0)] * 9 + [_close(-1.0)]
    assert kelly_fraction(fills) <= 1.0
```

**Step 2: Verify failure**

Run: `python3 -m pytest tests/backtest/test_kelly.py -v`
Expected: import error

**Step 3: Implement**

```python
# core/backtest/kelly.py
"""Kelly fraction from CLOSE Fill records.

Standard half-Kelly recommendation: multiply result by 0.5 for use.
"""
from typing import List
from core.reconciliation.base import Fill


def kelly_fraction(fills: List[Fill]) -> float:
    closes = [f for f in fills
              if f.opens_or_closes == "CLOSE" and f.realized_pnl_usd is not None]
    wins = [f.realized_pnl_usd for f in closes if f.realized_pnl_usd > 0]
    losses = [-f.realized_pnl_usd for f in closes if f.realized_pnl_usd < 0]
    if not wins or not losses:
        return 0.0
    p = len(wins) / len(closes)
    avg_win = sum(wins) / len(wins)
    avg_loss = sum(losses) / len(losses)
    b = avg_win / avg_loss
    f = (p * b - (1 - p)) / b
    return max(0.0, min(1.0, f))
```

**Step 4: Verify pass**

Run: `python3 -m pytest tests/backtest/test_kelly.py -v`
Expected: `4 passed`

**Step 5: Commit**

```bash
git add core/backtest/kelly.py tests/backtest/test_kelly.py
git commit -m "feat(backtest): Kelly fraction calc from Fill records"
```

---

## Task 10: Grid search + heatmap output

**Files:**
- Create: `core/backtest/grid_search.py`
- Create: `tests/backtest/test_grid_search.py`

**Step 1: Failing test**

```python
# tests/backtest/test_grid_search.py
import pandas as pd
from core.backtest.grid_search import grid_search


def test_grid_search_returns_one_row_per_param_combo():
    params = [{"score_min": 2.5}, {"score_min": 3.0}, {"score_min": 3.5}]
    bars = pd.DataFrame({"close": [1.0, 1.01, 1.02]})
    df = grid_search(
        bars=bars, param_grid=params,
        runner=lambda b, p: {"net_pnl": p["score_min"] * 0.1, "trades": 5, "wr": 0.4},
    )
    assert len(df) == 3
    assert "score_min" in df.columns
    assert "net_pnl" in df.columns


def test_grid_search_is_sorted_by_score_desc():
    params = [{"x": i} for i in [1, 5, 3, 2, 4]]
    df = grid_search(
        bars=pd.DataFrame({"close": [1.0]}),
        param_grid=params,
        runner=lambda b, p: {"net_pnl": p["x"], "trades": 1, "wr": 0.5},
        sort_by="net_pnl",
    )
    assert list(df["net_pnl"]) == [5, 4, 3, 2, 1]
```

**Step 2: Verify failure**

Run: `python3 -m pytest tests/backtest/test_grid_search.py -v`
Expected: import error

**Step 3: Implement**

```python
# core/backtest/grid_search.py
"""Grid search over strategy parameters."""
from typing import Callable, Dict, List
import pandas as pd


def grid_search(bars: pd.DataFrame, param_grid: List[dict],
                runner: Callable[[pd.DataFrame, dict], dict],
                sort_by: str = "net_pnl") -> pd.DataFrame:
    rows = []
    for params in param_grid:
        result = runner(bars, params)
        rows.append({**params, **result})
    df = pd.DataFrame(rows)
    if sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=False).reset_index(drop=True)
    return df
```

**Step 4: Verify pass**

Run: `python3 -m pytest tests/backtest/test_grid_search.py -v`
Expected: `2 passed`

**Step 5: Commit**

```bash
git add core/backtest/grid_search.py tests/backtest/test_grid_search.py
git commit -m "feat(backtest): grid search harness over strategy params"
```

---

## Task 11: Document + update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`
- Modify: `PROGRESS.md`
- Modify: `docs/CLEANUP_AFTER_CONFIRMATION.md`

**Step 1: Add backtest section to CLAUDE.md**

Append after the reconciliation section, before "Commands":

```markdown
## Backtest simulator (2026-04-XX)

**Lives at:** `core/backtest/` (Portfolio, ExchangeSim, runner, walk_forward, kelly, grid_search, compare)
**Tests:** `tests/backtest/` — every component TDD
**CLI:**
- `python3 scripts/run_backtest.py --symbol BTC-PERP --exchange nado --days 14` — backtest a strategy
- `python3.11 scripts/validate_strategy.py --exchange nado --symbol LIT-PERP --days 7` — **THE TRUST GATE**: sim vs live divergence

### How it works
- Event-driven loop processes 15m Binance candles bar-by-bar
- Per-exchange `ExchangeSpec` applies real fees (Nado 1/3.5 bps, Hibachi 0/35 bps, Paradex −0.5/2 bps)
- POST_ONLY rejection model: if limit crosses book, fill is None
- Output is `core.reconciliation.base.Fill` records — **same schema as live ledger**

### The trust protocol — DO NOT skip this before deploying a new strategy
1. Run backtest on last 30d historical → sim_fills
2. Paper-trade live for 3-7 days → ledger_fills
3. Run `validate_strategy.py` → if divergence > $1 OR > 5%, DO NOT deploy
4. After 14d soak with passing divergence: trusted
```

**Step 2: Add line to PROGRESS.md priority list**

```markdown
7. **Backtest simulator** — SHIPPED 2026-04-XX. See `core/backtest/`. Trust gate via `scripts/validate_strategy.py`.
```

**Step 3: Add cleanup item**

In `docs/CLEANUP_AFTER_CONFIRMATION.md`, add:

```markdown
### Old backtest scripts to retire after new sim is trusted

- [ ] `scripts/backtest_momentum.py` — uses lying gross PnL
- [ ] `scripts/strategy_backtest.py` — same
- [ ] `scripts/strategy_backtest_v2.py` — same
- [ ] `scripts/mcp_backtest.py` — same
- [ ] `scripts/self_learning_backtest.py` — review separately
- Replace all callers to use `core/backtest/runner.py`
```

**Step 4: Run full test suite to confirm nothing broken**

Run: `python3 -m pytest tests/ -v 2>&1 | tail -30`
Expected: all green (existing 62 reconciliation + new ~20 backtest)

**Step 5: Commit**

```bash
git add CLAUDE.md PROGRESS.md docs/CLEANUP_AFTER_CONFIRMATION.md
git commit -m "docs: document backtest simulator + trust protocol"
```

---

## Task 12: First real validation run (manual checkpoint)

After all tasks ship, the FIRST real use is a validation run on Hibachi BTC (the only profitable symbol per real ledger data):

```bash
python3.11 scripts/validate_strategy.py --exchange hibachi --symbol BTC/USDT-P --days 14
```

Expected output: sim_net and live_net should be within $1 OR 5%. If divergence > $1 AND > 5%, the simulator is wrong somewhere — likely fee schedule or POST_ONLY model. Do not trust any backtest until this passes.

This is a manual checkpoint — record the divergence number in PROGRESS.md.

---

## Out of scope (do NOT do in this PRD)

- Multiple concurrent positions across symbols (current Strategy interface is single-symbol — extend later if needed)
- Funding payment accrual (no live data feed yet — defer to Phase 2)
- Liquidation engine simulation (margin call modeling)
- Order book imbalance strategies (separate plan)
- Cross-sectional ranking strategies (separate plan)
- Removing the lying `pnl` field in JSONL — that's Phase 4 of the reconciliation plan

## Definition of done

- All 12 tasks committed
- `python3 -m pytest tests/backtest/ tests/reconciliation/ -v` → all green
- `scripts/run_backtest.py --symbol BTC-PERP --exchange nado --days 1` runs end-to-end and prints summary
- `scripts/validate_strategy.py --exchange hibachi --symbol BTC/USDT-P --days 7` runs end-to-end (may fail divergence — that's data, not a bug)
- CLAUDE.md and PROGRESS.md updated
- Cleanup list updated
