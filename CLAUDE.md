# Hopium Agents — AI Agent Guide

Live crypto trading bots across Paradex, Nado, Hibachi. As of 2026-04-17 on the reconciliation-first PnL architecture.

## Read first
- `PROGRESS.md` — current bot status, balances, baselines, what's running
- `docs/RECONCILIATION_PLAN.md` — architecture background
- `docs/CLEANUP_AFTER_CONFIRMATION.md` — what gets ripped out AFTER 48h soak confirms reconciler accuracy
- History before 2026-04-17: `docs/PROGRESS_ARCHIVE.md` (legacy PnL was fiction — do not use old numbers as baselines)

## Hard rules (do not violate)

- **Exchange = source of truth.** Query SDK for balance/positions/fills. Never trust `logs/momentum/*_trades.jsonl` `pnl` field (gross, fiction). Use `scripts/real_pnl.py` or the ledger.
- **Reconciliation baselines** live in `logs/reconciliation/{exchange}_soak.jsonl` line 1. Never infer baselines from pre-reconciler data.
- **Leverage-aware sizing**: buying power = equity × leverage. This bug has recurred 50+ times.
- **Reconciled positions** have entry_price=0. Guard every PnL calc.
- **Python 3.11** required for Paradex (SDK incompat with 3.9). Nado/Hibachi work on either.
- **Nado signer check BEFORE restart**: `verify_linked_signer()`. Toggling 1-Click Trading in Nado UI silently delinks (happened 3×).
- **Never hardcode position sizes.**
- **TDD for reconciliation code**: every new mapping/schema needs a test in `tests/reconciliation/` first. 55 tests baseline.
- **Timestamps are tz-aware UTC**: `datetime.now(timezone.utc)`. Dataclasses reject naive datetimes.
- **Never truncate addresses** in API calls.
- **Always update PROGRESS.md** when bot state changes (start/stop/config/balance).

## Reconciliation architecture (the truth layer)

```
core/reconciliation/
├── base.py      Reconciler ABC + frozen Fill/Position/ExchangeSnapshot/WindowPnL dataclasses
├── ledger.py    Append-only JSONL, fsync per write, dedup by (exchange, fill_id)
├── paradex.py   ParadexReconciler — fetch_fills/positions/account_summary
├── nado.py      NadoReconciler — Archive API matches + x18 scaling
└── hibachi.py   HibachiReconciler — raw GET /trade/account/trades

tests/reconciliation/     55 tests, TDD
scripts/real_pnl.py       honest cross-exchange PnL reporter
scripts/reconciler_soak.py  long-running read-only reconciler with drift alarm
logs/reconciliation/      {exchange}_soak.jsonl per-cycle snapshots
logs/ledger/              {exchange}_ledger.jsonl verified fills, append-only
```

### Per-exchange quirks

| Exchange | Python | Equity | Fills endpoint | Scaling | is_maker |
|---|---|---|---|---|---|
| Paradex | 3.11 only | `fetch_account_summary().account_value` | `fetch_fills()` → dict with `results` | Native USD strings | `liquidity == "MAKER"` |
| Nado | either | `healths[2].assets - liabilities` (x18) | `sdk._archive_query({"matches": ...})` | **All x18 scaled** — use `sdk._from_x18()` | `not is_taker` |
| Hibachi | either | `sdk.get_balance()` (float USD) | `sdk._request("GET", "/trade/account/trades", ...)` | Native USD strings | `not is_taker` |

### Sign conventions

```
fee             positive = we PAID (taker); negative = RECEIVED rebate (Paradex maker)
funding_paid    positive = we PAID; negative = RECEIVED  (our convention)
realized_pnl    gross of fees. None on OPEN fills.
net_pnl         realized - fee (per-fill) OR realized - fees - funding (window)
```

Paradex returns `realized_funding` positive=received — we INVERT when mapping to `funding_paid`.

## Backtest simulator (2026-04-20)

**Lives at:** `core/backtest/` — Portfolio, ExchangeSim, runner, momentum_strategy, walk_forward, kelly, grid_search, compare. 38 tests in `tests/backtest/`.

**CLI:**
- `python3 scripts/run_backtest.py --symbol BTC-PERP --exchange nado --days 14` — backtest momentum on Binance klines with per-exchange fees + POST_ONLY rejection
- `python3.11 scripts/validate_strategy.py --exchange hibachi --symbol BTC/USDT-P --days 7` — **THE TRUST GATE**: compares sim PnL to live ledger PnL. Exit 0 = trustworthy.

**Output schema:** `core.reconciliation.base.Fill` — same as live ledger. Sim and live are directly comparable.

**Trust protocol — BEFORE deploying any new strategy:**
1. Run `run_backtest.py` over last 30d → sim_fills
2. Paper-trade live 3-7 days → ledger_fills
3. Run `validate_strategy.py` → if divergence > $1 AND > 5%, DO NOT deploy
4. After 14d soak with passing divergence: trusted

**Old backtest scripts** (`scripts/backtest_momentum.py`, `strategy_backtest*.py`, `mcp_backtest.py`) use the lying gross `pnl` field. Retire after trust gate passes — see `docs/CLEANUP_AFTER_CONFIRMATION.md`.

## Commands

```bash
# Status — process + exchange equity + real PnL
ps aux | grep -E "momentum_mm|paradex_gpt|monitor\.py|reconciler_soak" | grep -v grep
python3.11 scripts/monitor.py --once
python3.11 scripts/real_pnl.py --hours 24

# Tests
python3 -m pytest tests/reconciliation/ -v

# Baselines (first snapshot per exchange)
head -1 logs/reconciliation/{exchange}_soak.jsonl | python3 -m json.tool

# Run bots (after Apr 13 restart pattern)
nohup python3 -u scripts/momentum_mm.py --exchange hibachi --assets all --interval 60 > logs/momentum/hibachi_bot.log 2>&1 &
nohup python3 -u scripts/momentum_mm.py --exchange nado --assets all --interval 60 > logs/momentum/nado_bot.log 2>&1 &
nohup python3.11 -u scripts/paradex_gpt_live.py --live --model qwen-max --interval 300 --size 15 > logs/paradex_live_v2.log 2>&1 &
nohup python3.11 -u scripts/monitor.py --interval 300 > logs/momentum/monitor.log 2>&1 &

# Run reconciler soaks (read-only, safe alongside live bots)
nohup python3.11 scripts/reconciler_soak.py --exchange paradex --interval 300 > logs/reconciliation/paradex_soak_runner.log 2>&1 &
nohup python3   scripts/reconciler_soak.py --exchange nado    --interval 300 > logs/reconciliation/nado_soak_runner.log 2>&1 &
nohup python3   scripts/reconciler_soak.py --exchange hibachi --interval 300 > logs/reconciliation/hibachi_soak_runner.log 2>&1 &

# Stop
pkill -f momentum_mm
pkill -f paradex_gpt_live
pkill -f reconciler_soak
```

## Exchange reference

| Exchange | Fees | Min | Notes |
|---|---|---|---|
| Paradex | 0% maker (rebates!), 0.02% taker | $10 | 3.11 required. POST_ONLY-only strategy ideal. |
| Nado | 1 bps maker, 3.5 bps taker | $100 notional | 10× leverage. Signer fragility — see hard rules. |
| Hibachi | 0% maker, 35 bps taker | — | 5× leverage. SDK doesn't expose POST_ONLY/ALO (~$0.20/day deferred). |

## Consulting Qwen

Use OpenRouter (`OPEN_ROUTER` key in `.env`). Model alias `qwen-max` → `qwen/qwen3-235b-a22b-2507` via `core/reconciliation` isn't the path — for strategy questions use a direct `requests.post` to `https://openrouter.ai/api/v1/chat/completions`. Don't pass OpenRouter model_id strings where `ModelClient` keys are expected (that bug bit us on 2026-04-13).

## Task Master

@./.taskmaster/CLAUDE.md
