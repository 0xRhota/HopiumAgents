# MCP Backtest Overnight Plan

> **For Claude:** Execute this plan autonomously overnight. No user approval needed.

**Goal:** Install free crypto MCP tools, pull signals retroactively for our 616 historical trades, and determine which signals would have improved PnL and volume on Nado/Hibachi.

**Architecture:** For each MCP tool, pull signal data for the timestamps of our real trades, then simulate: would the signal have correctly filtered bad trades or confirmed good ones? Compare to our v9 baseline.

**Tech Stack:** Python 3.9+, Node.js 18+, uv, existing trade JSONL files

---

## Phase 1: Install MCP Tools

### Task 1: Install funding-rates-mcp
```bash
cd /Users/admin/Documents/Projects/pacifica-trading-bot
git clone https://github.com/kukapay/funding-rates-mcp.git tools/funding-rates-mcp
cd tools/funding-rates-mcp && uv sync
```

### Task 2: Install crypto-indicators-mcp
```bash
cd /Users/admin/Documents/Projects/pacifica-trading-bot
git clone https://github.com/kukapay/crypto-indicators-mcp.git tools/crypto-indicators-mcp
cd tools/crypto-indicators-mcp && npm install
```

### Task 3: Install crypto-sentiment-mcp (skip if no Santiment API key)

## Phase 2: Build Retroactive Backtest Script

### Task 4: Create `scripts/mcp_backtest.py`

Load all 616 trades from JSONL files. For each trade:
1. Get the entry timestamp, symbol, side, entry_price, exit_price, exit_reason, pnl
2. Query each MCP tool for what signals existed at that timestamp
3. Record: would the signal have confirmed or vetoed the trade?
4. Calculate: if we'd filtered by that signal, what would aggregate PnL be?

### Task 5: Funding Rate Analysis
For each trade timestamp + symbol:
- Pull funding rates from multiple exchanges
- Classify: extreme positive (>+0.03%), extreme negative (<-0.03%), neutral
- Test hypothesis: trades aligned with funding rate bias → better WR?

### Task 6: Technical Indicator Cross-Validation
For each trade, pull from crypto-indicators-mcp:
- RSI (compare to our v9 RSI)
- MACD strategy signal (-1/0/1)
- Bollinger Band position
- ATR (volatility context)
- VWAP position
Test: would additional indicators have filtered losing trades?

### Task 7: Composite Signal Testing
Combine signals into composite filters:
- Filter A: Funding rate alignment + v9 score >= 3.0
- Filter B: MACD strategy agrees + v9 score >= 3.0
- Filter C: RSI + BB + volume confirmation
- Filter D: All of the above (strictest)

## Phase 3: Analysis & Report

### Task 8: Generate comparison report
For each filter:
- Trades taken vs baseline
- Win rate vs baseline
- Total PnL vs baseline
- Volume impact (trades/day)
- Best/worst asset performance

### Task 9: Save results to `research/mcp_backtest_results/`

### Task 10: Update SCRATCHPAD.md and PROGRESS.md with findings

## Premium MCP Research (docs only)

### Task 11: Research Cerebrus Pulse, SignalFuse, Coinversaa Pulse
- Read whatever docs/marketing exist
- Document: what they'd provide, cost model, integration effort
- Assess: worth paying for given our free tool findings?
