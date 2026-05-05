# Progress Tracker

**Last Updated:** 2026-05-04

Historical sessions prior to 2026-04-17 live at `docs/PROGRESS_ARCHIVE.md`. Pre-reconciler PnL numbers in that file are fiction — do not use as baselines.

---

## Active Processes (as of 2026-05-04 23:15 local)

### Live trading bots (real money, ~$50 total)

| Bot | Command |
|---|---|
| Hibachi momentum | `python3 scripts/momentum_mm.py --exchange hibachi --assets all --interval 60` |
| Nado momentum | `python3 scripts/momentum_mm.py --exchange nado --assets all --interval 60` |
| Paradex momentum | `python3.11 scripts/momentum_mm.py --exchange paradex --assets all --interval 60` |
| Monitor | `python3.11 scripts/monitor.py --interval 300` |
| Paradex reconciler soak | `python3.11 scripts/reconciler_soak.py --exchange paradex --interval 300` |
| Nado reconciler soak | `python3 scripts/reconciler_soak.py --exchange nado --interval 300` |
| Hibachi reconciler soak | `python3 scripts/reconciler_soak.py --exchange hibachi --interval 300` |

All running since Apr 30 / Apr 24 / Apr 21 — no crashes since Hibachi tick-size + Nado max-positions fixes.

### Paper trading sim (fictional accounts, no real money)

7 paper-sim processes started 2026-05-04 21:20 UTC running for one week pre-funding evaluation:

| Process | Account | Strategy | Capital |
|---|---|---|---|
| `paper_sim.cli calibrate --duration 24h` | — | Records live L2 + trades from Paradex + Hyperliquid | n/a |
| `paper_sim.cli run --account A` | A | Tight slow scanner, no LLM, top-20 Paradex perps, score≥4.5 | $5,000 |
| `paper_sim.cli run --account B` | B | BTC funding arbitrage HL+Paradex (delta-neutral) | $5,000 |
| `paper_sim.cli run --account C1` | C1 | Opus + DeepSeek-V3.1 consensus | $5,000 |
| `paper_sim.cli run --account C2` | C2 | Opus + Qwen3-Max consensus | $5,000 |
| `paper_sim.cli run --account C3` | C3 | Opus + Grok-4.20 consensus | $5,000 |
| `paper_sim.cli run --account D` | D | Hyperliquid BTC maker-rebate quoting | $5,000 |

**Status check:** `python3 -m paper_sim.cli report`. Ledgers under `logs/paper/{account}_ledger.jsonl`. LLM decision logs under `logs/paper/{account}_decisions.jsonl` (C1/C2/C3 only).

## paper_sim — new isolated package (2026-05-04)

```
paper_sim/                       NEW — does not import from core/, dexes/, scripts/
├── core/                        sim primitives
│   ├── types.py                 frozen dataclasses
│   ├── book.py                  L2 orderbook maintainer
│   ├── queue.py                 queue-position tracker (FIFO + price priority)
│   ├── fills.py                 FillEngine (single source of truth for "did this fill?")
│   ├── adverse.py               post-fill 30s mid-drift tracker
│   ├── latency.py               per-venue lognormal latency injector
│   ├── ledger.py                append-only JSONL with fsync + dedup
│   └── decision_log.py          LLM call + consensus capture
├── venues/                      data adapters (only I/O boundary)
│   ├── base.py                  VenueClient ABC
│   ├── replay.py                ReplayVenue + RecorderVenue
│   ├── paradex.py               live WS L2 + trades + funding
│   └── hyperliquid.py           live WS L2 + trades + funding
├── strategies/
│   ├── base.py                  Strategy ABC
│   ├── signals.py               RSI/MACD/EMA/ATR/momentum_score
│   ├── account_a_tight_slow.py  4-of-5 alignment, ≤5 trades/symbol/wk, 48h hold
│   ├── account_b_funding_arb.py BTC delta-neutral on funding spread > 3 bps
│   ├── account_c_llm_scout.py   2-LLM consensus, ≤10 trades/wk, 4h cadence
│   └── account_d_hl_maker.py    Both-sides quoting, inventory bands ±$2k
├── llm_clients.py               OpenRouter wrappers for Opus/DeepSeek/Qwen/Grok
├── runner.py                    orchestrates: WS → strategy → fills → ledger
├── reports.py                   PnL + adverse + volume breakdown
└── cli.py                       python -m paper_sim.cli {run,report,calibrate}

paper_sim/tests/                 126 passing, 1 skipped (calibration gate)
```

## Tracking Baselines — "since new reconciler system started"

The reconciliation ledger began 2026-04-17. Use these for honest "since we started tracking" deltas. Pre-reconciler data is unreliable.

| Exchange | Baseline ts (UTC) | Baseline equity | Source |
|---|---|---|---|
| Paradex | 2026-04-17 12:53:46 | $27.67 | `logs/reconciliation/paradex_soak.jsonl` line 1 |
| Nado | 2026-04-17 23:32:22 | $37.55 | `logs/reconciliation/nado_soak.jsonl` line 1 |
| Hibachi | 2026-04-17 23:32:22 | $22.81 | `logs/reconciliation/hibachi_soak.jsonl` line 1 |

Refresh: `head -1 logs/reconciliation/{exchange}_soak.jsonl | python3 -m json.tool`

## Reconciliation Architecture — Phases

| Phase | Status | Notes |
|---|---|---|
| 0 — Scaffold `core/reconciliation/` | ✅ done | Base ABC, dataclasses, ledger. 27 tests. |
| 1 — ParadexReconciler | ✅ done, live verified | 100 fills ingested, dedup verified |
| 2 — NadoReconciler | ✅ done, live verified | x18 scaling, Archive API wrapped |
| 3 — HibachiReconciler | ✅ done, live verified | Raw fills endpoint (not in SDK wrapper) |
| 3b — Hibachi POST_ONLY SDK extension | ⏸ deferred | $0.20/day value, not $25/day initial estimate |
| 4 — Rip out lying PnL code | ⏸ pending | Gated on 48h soak confirmation |
| 5 — Halt-on-divergence | ⏸ pending | After Phase 4 |

Tests: 55/55 pass. Run `python3 -m pytest tests/reconciliation/ -v`.

## Recent Fixes (2026-04-30 → 2026-05-04)

- **Hibachi tick-size rounding (2026-04-30)** — `dexes/hibachi/hibachi_sdk.py:create_limit_order` now rounds price to `tickSize` from market_info before signing. Previously close paths sent prices like 79160.2265 for BTC (tick=0.1), which Hibachi rejected with "Price ... is not a multiple of tick size 0.1".
- **Paradex slow preset confirmed (2026-04-30)** — config in `scripts/momentum_mm.py:307` was already on slow preset (max_hold=480m, score_min=3.5, max_positions=2, ATR exits 3.0/1.5). Killed legacy scalper PID and restarted clean. Trade rate ~14/day stems from 68-symbol universe, not from a different config.
- **Nado max_positions reduced 2→1 (2026-04-30)** — $100 min_notional × 10× leverage on $20-30 account meant 2 positions = 100% buying-power utilization, triggering account-health rejections on every new entry. 1 position ≈ 50% utilization, leaves margin for stops/funding.
- **paper_sim package built (2026-05-04)** — see new section above. 126 tests passing.

## Outstanding Priority Work

1. **Paper-sim 7-day evaluation** — currently running. End-of-week (Sunday 2026-05-11) will rank Accounts A/B/C1/C2/C3/D on PnL, Sharpe, fill quality, adverse selection, and total volume. Decisions on which to fund with real capital depend on these results.

2. **Shadow-mode calibration test** — pending. Once `cli calibrate --duration 24h` finishes (~21:20 UTC 2026-05-05), `paper_sim/tests/calibration/test_shadow_divergence.py` runs the actual paper-vs-live comparison. Median |bps| < 2 = paper PnL is trusted.

3. **Phase 4 — rip out lying PnL code** after 48h soak confirms reconciler accuracy. Checklist in `docs/CLEANUP_AFTER_CONFIRMATION.md`.

4. **Backtest simulator (SHIPPED 2026-04-20)** — see `core/backtest/`, 108 tests pass. Major finding from 30d sweep: SLOW preset wins on AAVE/AVAX/UNI/ETH only (4-of-33 profitable on Nado). FAST preset structurally negative across all 33 symbols.

5. **Map remaining unmapped Nado equity perps** — AAPL/TSLA/NVDA/etc have no Binance kline data and are not in the bot universe.

6. **1H EMA trend filter** — backtest showed +$0.37/day lift, not yet in live bot.

7. **Hibachi POST_ONLY/ALO SDK extension** — deferred, low urgency.

## Backlog — data sources / integrations parked

See `docs/BACKLOG.md` for full notes. Summary:

- **Massive.com data API** — API key in `.env`. Spot-CEX-only; missing perp signals we need.
- **Hyperliquid integration for live trading** — paper sim uses HL data; promoting to live execution is a future step gated on paper results.

## Oct 2025 Lighter Hack — Reference

Documented incident, $433.76 drained. Full audit: `docs/SECURITY_AUDIT_REPORT.md`. Recovered prompt history: `docs/OCT2025_CHAT_HISTORY_RECOVERED.md`.
