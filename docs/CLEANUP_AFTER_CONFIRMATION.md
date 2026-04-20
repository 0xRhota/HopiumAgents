# Cleanup List — DO NOT DELETE UNTIL CONFIRMED

**Purpose**: track every file / code block to rip out once the reconciliation-first architecture is proven working on all three exchanges.

**Rule**: nothing on this list is touched until the user says "confirmed, clean it up."

---

## Code to delete

### In `scripts/momentum_mm.py`

- [ ] **Lines 602–611**: `pnl_calc = (current_price - entry_p) * size` block. Replace with: read authoritative net PnL from ledger (written by reconciler after fill).
- [ ] **Line 611**: `pnl_balance_delta = equity_post_close - self.position["equity_before"]`. Always corrupted by other positions. Delete outright.
- [ ] **Line 633**: `"pnl_balance_delta": round(pnl_balance_delta, 6)` JSONL field. Delete.
- [ ] **Line 632**: `"pnl": round(pnl_calc, 6)` — rename to `"pnl_price_only_gross"` with a warning comment, or delete entirely if ledger covers analysis.
- [ ] **Line 648–651**: `self.learner.record_trade(asset, score, pnl_calc)` — swap `pnl_calc` for net PnL looked up from ledger.

### In `scripts/watchdog.py`

- [ ] **Line 201 area**: legacy `pnl_delta` fallback in `_tpnl()`. Delete once all history migrated or declared frozen.

### In `core/strategies/momentum/self_learning.py`

- [ ] **Line 94**: `pnl = trade.get("pnl") or trade.get("pnl_delta", 0.0)` — change to pull `pnl_net` from ledger.
- [ ] **Lines 104–118**: circuit breaker + score bucket logic reads `pnl`; update to read `pnl_net` signed by ledger.

### In `scripts/mcp_backtest.py`

- [ ] **Line 80**: `t["pnl"] = t.get("pnl", t.get("pnl_delta", 0))` legacy fallback. Decide: keep for backtest historical data (OK) or remove.

### Analysis scripts that aggregate JSONL (lies)

- [ ] `scripts/analyze_all_trades.py` — rewrite to call exchange APIs via reconciler, or delete if redundant.
- [ ] `scripts/analyze_profitability.py` — rewrite similarly.
- [ ] `scripts/analyze_v2_performance.py` — rewrite similarly.
- [ ] `scripts/analyze_deep42_impact.py` — probably still relevant for Deep42 comparison. Keep but migrate data source.

### Hibachi POST_ONLY — DEFERRED (revised priority)

**Finding 2026-04-17 from hibachi reconciler**: live fills show 63% maker / 37% taker. Avg fee per fill $0.0055. Projected savings from forcing 100% maker-only: ~$0.20/day at current volume (was initially estimated at $25/day, which was wrong — I assumed all fills were taker). Still worth doing but no longer high-priority.

- [ ] Extend `dexes/hibachi/hibachi_sdk.py` with POST_ONLY / ALO order type (needs binary-signed order format reverse-engineered; Hibachi docs mention ALO but don't document API).
- [ ] Flip `HibachiAdapter.supports_post_only = True` after SDK extension.

### Paradex bot — URGENT

**Finding 2026-04-17 from paradex ledger**: 100 consecutive fills = 100 TAKER, 0 MAKER. Paradex pays rebates for maker orders. We've been paying taker fees on an exchange that would literally PAY us to post liquidity.

- [ ] When building the Paradex momentum adapter (plan Phase 1+), use POST_ONLY exclusively. Do NOT let it fall back to taker. If POST_ONLY rejects, skip the cycle.
- [ ] Current `scripts/paradex_gpt_live.py` Qwen bot also places takers. Either switch it to POST_ONLY or decommission it when momentum adapter ships.

### Old backtest scripts to retire once new sim is trusted

Trust gate: `scripts/validate_strategy.py` must return exit 0 on ≥1 symbol. Then retire the below because they all use the lying gross `pnl` from JSONL:

- [ ] `scripts/backtest_momentum.py` — uses lying gross PnL
- [ ] `scripts/strategy_backtest.py` — same
- [ ] `scripts/strategy_backtest_v2.py` — same
- [ ] `scripts/mcp_backtest.py` — same
- [ ] `scripts/self_learning_backtest.py` — review separately (uses same field for WR calc)

Replace callers with `python3 scripts/run_backtest.py --symbol X --exchange Y` and read results from emitted Fill records.

### Hibachi SDK

- [ ] `dexes/hibachi/hibachi_sdk.py:564` — update comment that says "POST_ONLY (ALO) is documented ... but not exposed in the API" once we DO expose it.
- [ ] `dexes/hibachi/hibachi_sdk.py:611-612` — `max_fees_int = int(0.005 * (10 ** 8))` hard-coded taker max. If maker path added, conditionally skip this.

### ExchangeAdapter

- [ ] `core/strategies/momentum/exchange_adapter.py:134` — `HibachiAdapter.supports_post_only = False`. Flip to True once SDK supports it.

---

## Data to mark as "pre-cutover suspect"

- [ ] All `logs/momentum/*_trades.jsonl` entries written BEFORE reconciler cutover date. Any analysis over pre-cutover data must apply a known-lies warning label.

---

## Dependencies between cleanup items

1. Do NOT delete `pnl` field from JSONL until self_learning.py migrated (it reads `pnl`).
2. Do NOT flip Hibachi `supports_post_only = True` until SDK actually supports it AND soak tested.
3. Do NOT remove `pnl_delta` legacy fallback until all old JSONL history is archived/frozen.

---

## Confirmation criteria (what "confirmed, clean it up" means)

Before touching this list:
1. All three reconcilers running in parallel with live bots for minimum 48h
2. Reconciler-reported equity matches exchange dashboard within $0.05 on all three
3. No DRIFT_ALARM entries in last 24h of reconciliation log
4. User explicitly says OK to clean up

Once those four are true → execute this list in phases, testing after each.
