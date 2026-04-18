# Reconciliation-First PnL Architecture — Plan

**Status**: DRAFT (pending user approval before any code changes)
**Date**: 2026-04-17
**Author**: Claude
**Scope**: Full rewrite of PnL tracking across Paradex, Nado, Hibachi

---

## The problem in one sentence

The bot computes its own PnL from `(exit_price − entry_price) × size`, which ignores fees, slippage, funding, and open-position unrealized PnL. Over 37 Nado trades yesterday, the bot reported +$0.19 realized gross while exchange equity fell ~$14. That is a 7000% divergence. At $5000 account size, same architecture silently bleeds ~$1000/day uncaught.

---

## Research findings

### Per-exchange capability matrix (verified live 2026-04-17)

| Capability | **Paradex** | **Nado** | **Hibachi** |
|-----------|-------------|----------|-------------|
| Account equity | `fetch_account_summary().account_value` ✅ | `get_subaccount_info()` (healths calc) ✅ | `get_balance()` ✅ |
| Open positions | `fetch_positions()` with `results[]` ✅ | `get_positions()` (no entry_price ❌) | `get_positions()` with `openPrice` ✅ |
| **Fills with fees** | `fetch_fills()` with per-fill `realized_pnl` ✅ (need to verify fee field) | **Archive API via `get_pnl(hours=N)` → realized_pnl + fees + net_pnl** ✅ | **No fills endpoint in SDK wrapper** ❌ |
| Funding history | Has endpoint (not yet used) ⚠️ | Not implemented ❌ | Not implemented ❌ |
| POST_ONLY (maker) | ✅ Yes, 0% maker / 0.02% taker | ✅ Yes, 1 bps maker / 3.5 bps taker | ⚠️ Hibachi docs support ALO but SDK wrapper doesn't expose it. Currently paying ~35 bps taker on exits |

**Live probe results (today)**:
- Hibachi balance call: `23.645336` — works
- Nado `get_pnl(hours=2)`: `{realized_pnl: -2.9244, fees: 0.2298, net_pnl: -3.154}` — works, this is the model for everything else
- Paradex fills: authenticated, returns `realized_pnl` per fill — works

### What's lying right now

Every site that sums `trade['pnl']` from `logs/momentum/*_trades.jsonl` is lying. That includes:

| File | Line | What it does |
|------|------|--------------|
| `scripts/momentum_mm.py` | 607–637 | Computes `pnl_calc = (exit - entry) × size`, writes it as `pnl` field |
| `core/strategies/momentum/self_learning.py` | 94, 104–118 | Feeds `pnl` into circuit breaker + score-bucket WR. **The self-learning gate blocks trades based on bad data.** |
| `scripts/analyze_all_trades.py` | 57–70 | Sums `pnl` for analysis |
| `scripts/analyze_profitability.py` | 15–27 | Same |
| `scripts/analyze_v2_performance.py` | — | Same |
| `scripts/watchdog.py` | 64–79 (status) | Queries exchange (GOOD, keep) |
| `scripts/pnl_tracker.py` | 71–152 | Queries Nado/Paradex exchange APIs (GOOD, existing pattern to mirror) |

### Score-bucket self-learning contamination — example

- Logged: score 3.5 bucket = 47% WR → threshold 25% → **ALLOW trading**
- Actual (after fees): ~30% WR → borderline
- Current behavior: keeps trading on a near-breakeven bucket, lies about profitability

### What must be ripped out

1. **`pnl_balance_delta` field** (`momentum_mm.py:614, 637`) — never consumed, corrupted by other positions. Delete.
2. **`pnl` as "realized PnL" semantic** — keep the field but rename to `pnl_price_only` and clearly label as gross-of-fees. Add `pnl_net` as the real number.
3. **Legacy `pnl_delta` fallbacks** in `self_learning.py:96`, `mcp_backtest.py:80`, `watchdog.py:201` — once new schema lands, delete.
4. **All `analyze_*.py` that aggregate JSONL** — rewrite to call exchange APIs.
5. **Hibachi `supports_post_only = False`** — reassess (see maker-only section below).

---

## Maker-only feasibility (your first question)

Goal: avoid taker fees to enable higher-volume trading.

### Nado — **DO IT**
- Entries currently use non-POST_ONLY limit orders → taker fees when book crosses (saw 2008 "post-only crosses book" errors earlier, so some are already POST_ONLY — mixed).
- Close path already tries POST_ONLY first (`exchange_adapter.py:446, 474`), falls back to market.
- **Plan**: Add `order_type="POST_ONLY"` to entry orders too. Accept higher rejection rate as the cost of lower fees. If POST_ONLY rejects, DON'T fall back to taker — skip the entry cycle.
- **Impact**: 3.5 bps → 1 bps per side = 2.5 bps saved × 2 sides × $100 = $0.05/trade. At 142 trades/24h = $7.10/day saved. Roughly offsets the gap we're seeing.

### Paradex — **IDEAL**
- 0% maker, rebates paid on fills. This is the cheapest venue.
- Not currently on momentum strategy (has a separate Qwen bot). Paradex momentum adapter was already on priority list.
- **Plan**: When building ParadexAdapter for momentum, force POST_ONLY. Failed POST_ONLY = skip cycle.

### Hibachi — **REQUIRES SDK WORK**
- SDK wrapper at `dexes/hibachi/hibachi_sdk.py:564` explicitly notes: "POST_ONLY (ALO) is documented in Hibachi conceptual docs but not exposed in the API."
- Current exit path uses **market orders** = full 35 bps taker. This is the single biggest fee sink in the whole system.
- **Plan**: Either (a) extend Hibachi SDK wrapper to support ALO order type via raw HTTP if the exchange supports it, or (b) replace market exits with aggressive limit orders at the opposing best price (still maker-eligible on IOC-like flows).
- **Impact**: 35 bps → 0 bps on exit = $0.35/trade × 73 Hibachi trades/24h = **$25/day saved**. Largest single lever in the system.
- **Risk**: Higher exit latency, some exits won't fill and positions stay open past max_hold. Need to test.

### Maker-only summary
- Nado: easy win, ~$7/day saved
- Paradex: build the adapter with POST_ONLY-only from day 1
- Hibachi: biggest savings ($25/day) but requires SDK extension and risk testing

---

## Proposed architecture

### Principle
**Exchange = source of truth. Bot state = cache that must be verified every cycle.**

### New abstract layer: `Reconciler`

```
core/reconciliation/
├── base.py            # Reconciler ABC
├── paradex.py         # ParadexReconciler
├── nado.py            # NadoReconciler (wraps existing get_pnl)
├── hibachi.py         # HibachiReconciler (builds fills query)
└── ledger.py          # Persistent truth store
```

### `Reconciler` interface (per exchange)

```python
class Reconciler(ABC):
    @abstractmethod
    async def snapshot(self) -> ExchangeSnapshot:
        """Full authoritative state from exchange.
        
        Returns:
            equity: account_value from exchange
            positions: List[Position] with {symbol, side, size, entry_price, unrealized_pnl}
            fills_since: List[Fill] with {symbol, side, size, price, fee, funding, ts, fill_id}
                where 'since' = last snapshot timestamp
            funding_paid_since: float (total across positions)
            ts: exchange-reported timestamp
        """
    
    @abstractmethod
    async def get_pnl_window(self, hours: int) -> WindowPnL:
        """Historical PnL over time window.
        
        Returns:
            realized_pnl: sum across fills
            fees_paid: sum across fills (signed: positive for paid, negative for rebate)
            funding_paid: sum of funding payments
            net_pnl: realized - fees + funding
            trade_count: int
            window_start: datetime
            window_end: datetime
        """
```

### Cycle integration (replaces current momentum_mm flow)

Every interval (60s default):
1. `snapshot = await reconciler.snapshot()` — authoritative state
2. Detect new fills since last snapshot → append to Ledger with full context
3. Compare `snapshot.equity` to `cached_equity + sum(new_fill.net_pnl)`. If drift > $0.05 per fill or > $1 cumulative, log DRIFT_ALARM.
4. Pass `snapshot.positions` (with real entry_price from exchange) to strategy — NOT the bot's internal position state.
5. Strategy decides; bot places orders; order placement returns an order_id.
6. Next cycle, reconciler finds the fill(s) matching that order_id and records actual fill price + fee.

### Ledger (the new persistent truth store)

`logs/ledger/{exchange}_ledger.jsonl` — append-only, each line is a verified fill:

```json
{
  "ts": "2026-04-17T14:23:45Z",
  "exchange": "nado",
  "symbol": "LIT-PERP",
  "fill_id": "0x76419619...",
  "order_id": "0x76419619...",
  "side": "LONG",
  "size": 109.0,
  "price": 1.0215,
  "notional_usd": 111.35,
  "fee": 0.039,
  "is_maker": true,
  "realized_pnl_usd": null,          // null for opening fill
  "opens_or_closes": "OPEN",
  "linked_entry_fill_id": null,
  "equity_before_fill": 52.91,       // from snapshot
  "equity_after_fill": 52.87,         // from snapshot
  "source": "reconciler.nado.fills_since"
}
```

For CLOSING fills: `realized_pnl_usd` is populated from `(exit_price - entry_price) × size - fees_on_this_fill`, where entry_price is looked up via `linked_entry_fill_id`. This is the ONLY place `pnl` appears, and it's computed from exchange-reported fill data, not bot memory.

### Drift detection

Every cycle:
```
expected_equity = session_start_equity + sum(ledger[since_session_start].realized_pnl_usd) 
                  - sum(fees) + sum(funding) + sum(unrealized from open positions)
actual_equity = snapshot.equity
drift = abs(actual_equity - expected_equity)
if drift > max(1.00, 0.02 * actual_equity):
    logger.error(f"DRIFT_ALARM: expected {expected_equity:.4f} actual {actual_equity:.4f} drift ${drift:.4f}")
    # Optional: halt trading until human review
```

### Self-learning fix

`MomentumLearner.record_trade()` should read from Ledger (`pnl_net`) not from the momentum bot's computed value. Score-bucket WR becomes honest.

---

## Implementation sequence

### Phase 0 — Prepare (no bot disruption)
- Write `core/reconciliation/base.py` with `Reconciler` ABC, `ExchangeSnapshot`, `Fill`, `WindowPnL` dataclasses.
- Write `core/reconciliation/ledger.py` — append-only JSONL writer + query helpers.
- **No code is wired in yet.**

### Phase 1 — Paradex (pilot)
- Why first: already has clean fills API (`fetch_fills()` returns `realized_pnl` per fill); Paradex bot has been idle holding 1 BTC LONG all week so it's low-risk to touch.
- Implement `ParadexReconciler` using existing `ParadexSubkey` auth pattern from `scripts/pnl_tracker.py:100-152`.
- Verify `fetch_fills()` includes fee field per fill (need to inspect one fill schema). If fees aren't in fills, compute from `0% maker / 0.02% taker` based on `is_maker` flag.
- Run read-only in parallel with the existing Paradex bot for 24h. Compare Reconciler outputs to exchange account dashboard. **Green light criterion**: drift < $0.05 after 24h.
- Then cut Paradex momentum adapter. Wire it into `momentum_mm.py` as a new exchange option.

### Phase 2 — Nado
- Implement `NadoReconciler` wrapping `nado_sdk.get_pnl()` + extend to per-fill detail (need to call Archive `matches` endpoint ourselves, not just aggregate).
- Verify reconciler matches exchange dashboard over 24h.
- Enable maker-only entries on Nado bot (add `order_type="POST_ONLY"` to entries). Watch rejection rate.
- Cut over Nado bot to reconciler-driven state (strategy reads positions from reconciler, not from in-memory `self.position`).

### Phase 3 — Hibachi
- Hardest. No fills endpoint in our SDK wrapper.
- **Option A**: Investigate Hibachi's HTTP API directly for a fills/matches endpoint. If one exists but wrapper doesn't expose it, extend the wrapper.
- **Option B**: If no fills endpoint exists, reconcile from order events + balance deltas only. Less precise but workable.
- Add POST_ONLY / ALO order type to Hibachi SDK wrapper (biggest ROI item in whole plan — $25/day fee savings).
- Cut Hibachi bot over to reconciler.

### Phase 4 — Rip out the old
- Delete `pnl_balance_delta` field.
- Rename `pnl` → `pnl_price_only_gross` in schema.
- Update `self_learning.py` to read `pnl_net` from ledger.
- Rewrite `analyze_*.py` to query ledger + exchange APIs.
- Delete legacy `pnl_delta` fallback paths.

### Phase 5 — Halt-on-divergence
- Add `DRIFT_ALARM` → trading halt after N consecutive large-drift cycles. Opt-in via config flag first, default on after soak period.

---

## Testing strategy

For each phase:

1. **Unit tests** — mock exchange responses, verify reconciler parses correctly, computes ledger entries, detects drift when injected.
2. **Read-only live run** — reconciler runs alongside live bot in parallel (doesn't touch trading). Output dumped to a separate log. User compares to exchange dashboard after 24h.
3. **Green light criteria before cutover**:
   - Total reconciler-reported equity matches exchange within $0.05 over 24h
   - Every fill in exchange dashboard appears in our ledger (no missed fills)
   - No drift alarms during normal operation
4. **Cutover** — point strategy at reconciler, disable old pnl_calc.
5. **Soak** — 48h observation. Any drift alarm = rollback.

---

## What this does NOT solve

Honesty list:
- **Doesn't fix trading strategy.** Chop is still chop. Getting honest numbers lets you iterate on strategy, but this isn't the strategy fix.
- **Doesn't retroactively correct JSONL.** Historical lies stay in the record. We mark the cutover date and treat pre-cutover data as suspect.
- **Doesn't eliminate latency between fill and snapshot.** Exchanges have 1-5s lag on fills. Reconciler polls; a fast close right after open may appear in the next cycle's snapshot, not this one. Drift alarm threshold accounts for this.
- **Doesn't change that Hibachi has no fills API.** If option A fails we ship option B (balance-delta-based reconciliation), which is weaker than Nado/Paradex precision.

---

## Open questions for you

1. **Halt-on-divergence**: do you want the bot to AUTO-HALT on drift alarms, or just log them loudly? Auto-halt is safer for scaling to $5k+; at $50 it'd cause annoying pauses.
2. **Maker-only Nado**: accept higher rejection rate in exchange for 3.5bps→1bps savings? (I recommend yes.)
3. **Hibachi POST_ONLY**: worth ~$25/day at current volume but requires SDK extension. Sequence: after Paradex pilot, before or during Hibachi phase?
4. **Scope of the rewrite**: do we ALSO rewrite the analyze_*.py scripts in Phase 4, or leave that for after live bots are reconciled?

---

## Effort estimate (rough)

- Phase 0 (scaffolding): 3-4 hours
- Phase 1 (Paradex pilot + soak): 1 day work + 1 day soak
- Phase 2 (Nado): 1 day + 1 day soak
- Phase 3 (Hibachi): 2-3 days (SDK work + soak)
- Phase 4 (rip out old): 1 day
- Phase 5 (halt-on-divergence): 0.5 day
- **Total**: ~1 week of focused work + soak windows

---

## Deliverables per phase

Each phase ends with:
1. Working code with tests
2. Running read-only reconciler output for 24h captured in `logs/reconciliation/{exchange}_soak.log`
3. User approval before cutover based on drift numbers
4. PROGRESS.md updated
5. Rollback plan documented
