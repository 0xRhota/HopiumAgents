# paper_sim

Realistic paper-trading simulator for perpetual futures. Runs against live exchange data
(Paradex, Hyperliquid L2 WebSocket) and models execution effects that simple mid-price
sims miss: queue position, adverse selection, latency, real bid/ask spreads, funding-at-fill.

**Isolated from the rest of this repo.** Does not import from `core/strategies/`,
`scripts/`, or any momentum bot code. Logs to `logs/paper/` only.

## Run

```bash
# Tests
pytest paper_sim/tests/

# Shadow-mode calibration (records live data, predicts fills for existing live bots)
python -m paper_sim.cli calibrate --duration 24h

# Run the 4 paper accounts
python -m paper_sim.cli run --account A
python -m paper_sim.cli run --account B
python -m paper_sim.cli run --account C
python -m paper_sim.cli run --account D
```

## Architecture

```
core/   sim primitives (pure logic + types) — no I/O except ledger
venues/ data adapters — only I/O boundary; one file per venue
strategies/ pure functions: (market_state, portfolio) → list[IntendedOrder]
runner.py orchestrates: WS data → strategy → fill engine → ledger
```

## Key invariants

- All sim types are frozen dataclasses
- FillEngine is the single source of truth for "did this order fill?"
- PaperLedger is append-only JSONL with fsync per write
- AdverseSelectionTracker annotates fills, never decides them
- Strategies have no side effects; runner orchestrates all I/O
