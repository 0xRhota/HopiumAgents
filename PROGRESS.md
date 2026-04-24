# Progress Tracker

**Last Updated:** 2026-04-17

Historical sessions prior to 2026-04-17 live at `docs/PROGRESS_ARCHIVE.md`. Pre-reconciler PnL numbers in that file are fiction — do not use as baselines.

---

## Active Processes (as of 2026-04-17 20:32 local)

| Bot | PID | Command |
|---|---|---|
| Hibachi momentum | 33479 | `python3 scripts/momentum_mm.py --exchange hibachi --assets all --interval 60` |
| Nado momentum | 33636 | `python3 scripts/momentum_mm.py --exchange nado --assets all --interval 60` |
| Paradex Qwen swing | 26051 | `python3.11 scripts/paradex_gpt_live.py --live --model qwen-max --interval 300 --size 15` |
| Monitor | 16629 | `python3.11 scripts/monitor.py --interval 300` |
| Paradex reconciler soak | 1495 | `python3.11 scripts/reconciler_soak.py --exchange paradex --interval 10` |
| Nado reconciler soak | 91707 | `python3 scripts/reconciler_soak.py --exchange nado --interval 300` |
| Hibachi reconciler soak | 91810 | `python3 scripts/reconciler_soak.py --exchange hibachi --interval 300` |

All 7 alive since Apr 13 main-bot restart + Apr 17 soak adds. No crashes.

Uptime source of truth: `ps aux | grep -E "momentum_mm|paradex_gpt|monitor|reconciler_soak" | grep -v grep`.

## Tracking Baselines — "since new reconciler system started"

The reconciliation ledger began 2026-04-17. Use these for honest "since we started tracking" deltas. Pre-reconciler data is unreliable.

| Exchange | Baseline ts (UTC) | Baseline equity | Source |
|---|---|---|---|
| Paradex | 2026-04-17 12:53:46 | $27.67 | `logs/reconciliation/paradex_soak.jsonl` line 1 |
| Nado | 2026-04-17 23:32:22 | $37.55 | `logs/reconciliation/nado_soak.jsonl` line 1 |
| Hibachi | 2026-04-17 23:32:22 | $22.81 | `logs/reconciliation/hibachi_soak.jsonl` line 1 |

Refresh: `head -1 logs/reconciliation/{exchange}_soak.jsonl | python3 -m json.tool`

## Current Equity & PnL

Source: `python3.11 scripts/real_pnl.py --hours 24`. Queries exchange APIs directly.

```
Exchange   Equity   24h Realized   24h Fees   24h Net   Trades
paradex    $27.80   +$0.06         $0.00      +$0.06    2
nado       $37.54   +$0.66         $2.02      -$1.36    97
hibachi    $22.86   -$0.35         $0.34      -$0.68    64
───────────────────────────────────────────────────────────────
TOTAL      $88.21                             -$1.98
```

## Reconciliation Architecture — Phases

| Phase | Status | Notes |
|---|---|---|
| 0 — Scaffold `core/reconciliation/` | ✅ done | Base ABC, dataclasses, ledger. 27 tests. |
| 1 — ParadexReconciler | ✅ done, live verified | 100 fills ingested, dedup verified |
| 2 — NadoReconciler | ✅ done, live verified | x18 scaling, Archive API wrapped |
| 3 — HibachiReconciler | ✅ done, live verified | Raw fills endpoint (not in SDK wrapper) |
| 3b — Hibachi POST_ONLY SDK extension | ⏸ deferred | $0.20/day value, not $25/day initial estimate |
| 4 — Rip out lying PnL code | ⏸ pending | **Gated on 48h soak confirmation** |
| 5 — Halt-on-divergence | ⏸ pending | After Phase 4 |

Tests: 55/55 pass. Run `python3 -m pytest tests/reconciliation/ -v`.

## Known Findings from Real Ledger Data

- **Paradex**: 100 TAKER / 0 MAKER in last 100 fills. Paradex pays maker rebates. When building Paradex momentum adapter, use POST_ONLY exclusively.
- **Nado**: 57% maker / 43% taker. Fee schedule healthy (~$10/500 fills). Yesterday's $14 overnight loss was strategy chop, not fees.
- **Hibachi**: 63% maker / 37% taker. Fee impact tiny. Revised down from initial $25/day savings estimate.

## Outstanding Priority Work

1. **Backtest simulator — SHIPPED 2026-04-20**. See `core/backtest/`, `scripts/run_backtest.py`, `scripts/validate_strategy.py`, `scripts/strategy_sweep.py`. 108 tests pass.

   **Major finding 2026-04-20 from full Nado sweep (33 symbols × 2 configs × 30d):**
   - FAST preset (current live: score≥2.5, 80/40 bps): **0 of 33 symbols profitable**, aggregate NET **−$1,568**
   - SLOW preset (Paradex-style: score≥3.5, ATR×3/×1.5, 10% size): **4 of 33 profitable**, aggregate NET **−$635**
   - Slow saves $933 of bleed (60% less loss) vs fast
   - Winning symbols on slow: **AAVE +$16, AVAX +$12, UNI +$2, ETH +$2** (30d). Near break-even: ZRO, SKY.
   - Disproven: user hypothesis that lower-liq/newer symbols have edge. The edge lives in established mid-caps; newer tokens (PUMP, PENGU, WLFI) are catastrophic bleeders.
   - Saved: `logs/sweeps/nado_sweep_20260420T163031.csv`

   **Calibration tasks before sim is trusted:**
   - Make `post_only` decision exchange-aware in BacktestMomentumStrategy (Hibachi → False, Nado/Paradex → True)
   - Strengthen `compare_pnl` to fail when trade-count divergence > X%
   - Re-run validation on Nado AAVE; close gap before trusting sweep $ figures
   - PUMP/PENGU 0% WR likely a kilo-token Binance mapping bug, not strategy finding

2. **Maker-only close — SHIPPED 2026-04-20.** Removed `create_market_order` fallback on Hibachi + Nado close paths. Widening-limit loop instead. 3 regression tests verify no market orders called. User directive: no taker fees ever.

3. **ATR-adaptive TP/SL — SHIPPED 2026-04-20.** Replaced fixed 80/40 bps with `TP = max(60bps, 2×ATR), SL = max(30bps, 1×ATR)`. Live on Hibachi + Nado. Startup banner + per-symbol score log now show ATR values.

4. **Nado live symbol discovery — SHIPPED 2026-04-20.** Added `NadoSDK.fetch_symbols_map()` querying `/symbols` endpoint with 1h TTL. Bot discovered +32 markets that hardcoded dict missed (AAPL, AMZN, MSFT, GOOGL, META, NVDA, TSLA + QQQ, SPY, EURUSD, GBPUSD, USDJPY, XAG, WTI + 15 alt-coins).

5. **Hibachi pagination — SHIPPED 2026-04-20.** Reconciler now pages `/trade/account/trades` via `endTime` cursor; can fetch real 14d window instead of top-100 cap.

2. **Phase 4 — rip out lying PnL code** after 48h soak confirms reconciler accuracy. Checklist in `docs/CLEANUP_AFTER_CONFIRMATION.md`.
2. **Fund Hibachi** — at $22.81, bleeding slowly.
3. **Map 29 new Nado products** — includes 14 equity perps (AAPL/TSLA/NVDA/etc.). `dexes/nado/nado_sdk.py PRODUCT_SYMBOLS`.
4. **1H EMA trend filter** — backtest showed +$0.37/day lift, not in live bot.
5. **Paradex momentum adapter** — put Paradex on momentum strategy, POST_ONLY only.
6. **Hibachi POST_ONLY/ALO SDK extension** — deferred, low urgency.

## Backlog — data sources / integrations parked for now

See `docs/BACKLOG.md` for full notes. Summary:

- **Massive.com data API** (evaluated 2026-04-21) — API key in `.env` as `MASSIVE_API_KEY`. Spot-CEX-only provider; missing the perp signals we actually need (funding, OI, liquidations, L2). Possible future uses: basis-divergence signal between aggregated CEX spot FMV and our perp price; cross-venue volume attribution; MCP integration for ad-hoc research queries. **Better alternatives for perp-specific gaps**: Coinglass (funding/OI/liquidations, free tier), Binance/Bybit direct REST.

## Oct 2025 Lighter Hack — Reference

Documented incident, private key exposed in chat during Lighter SDK setup, $433.76 drained from `0xCe9784FcDaA99c64Eb88ef35b8F4A5EabDC129d7`. Full audit: `docs/SECURITY_AUDIT_REPORT.md`. Recovered prompt history: `docs/OCT2025_CHAT_HISTORY_RECOVERED.md`.
