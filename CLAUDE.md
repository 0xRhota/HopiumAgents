# Hopium Agents - AI Agent Guide

**Read [Project_Analysis.md](Project_Analysis.md) first.**

16,803 trades. 50+ strategy iterations. Oct 2025 to Jan 2026.

---

## Key Data

### Confidence vs Actual Win Rate

| Confidence | Expected WR | Actual WR | Gap |
|------------|-------------|-----------|-----|
| 0.6 | 60% | 46.2% | -13.8% |
| 0.7 | 70% | 44.7% | **-25.3%** |
| 0.8 | 80% | 44.2% | **-35.8%** |
| 0.9 | 90% | 51.7% | **-38.3%** |

0.8 confidence = 44% actual win rate. Don't size based on confidence.

### What Works
1. Require score >= 3.0 before trading (v9 system)
2. SHORT bias on Lighter/Extended (SHORT WR: 49.4% vs LONG: 41.8%)
3. Hard exit rules: +2%/-1.5%, 2h min hold
4. 2-5 positions max
5. Asymmetric R:R (30% WR works if winners are 3x losers)

### What Doesn't Work
1. Wider Grid MM spreads (v2-v4 proved it)
2. Deep42 for exit decisions (causes early exits)
3. Sizing up on high confidence
4. BCH, BNB, ZEC trading
5. Time-based Grid refresh (use 0.25% price trigger)

---

## Quick Navigation

| Document | Purpose |
|----------|---------|
| [LEARNINGS.md](LEARNINGS.md) | **Central learnings log** (update after every discovery) |
| [PROGRESS.md](PROGRESS.md) | Current bot status and configurations |
| [Project_Analysis.md](Project_Analysis.md) | Trade data, strategy iterations, what works |
| [README.md](README.md) | Project overview and quick start |
| [CONTRIBUTING.md](CONTRIBUTING.md) | For colleagues contributing code |
| [research/Learnings.md](research/Learnings.md) | Detailed strategy history |

---

## Core Mission

**"We feed them a variety of quantitative data that tries to capture the 'state' of the market at different granularities. Funding rates, OI, volume, RSI, MACD, EMA, etc"**

### Data Sources (All Required in Every Decision)
- **Funding rates** - Perpetual futures funding (long/short bias)
- **Open Interest (OI)** - Total open positions (market leverage)
- **Volume** - 24h trading volume (liquidity/momentum)
- **RSI** - Relative Strength Index (overbought/oversold)
- **MACD** - Moving Average Convergence Divergence (trend strength)
- **EMA/SMA** - Exponential/Simple Moving Averages (trend direction)
- **Deep42 Sentiment** - AI market intelligence (for entries only, NOT exits)
- **Price** - Current spot price

**Every decision cycle MUST log this data summary BEFORE the LLM decision.**

---

## Exchange Performance Summary

| Exchange | Trades | Win Rate | Best Direction | Fees |
|----------|--------|----------|----------------|------|
| Lighter | 12,665 | 44.7% | SHORT (49.4%) | 0%/0% |
| Hibachi | 1,579 | 37.3% | LONG (38.9%) | Low |
| Extended | 1,590 | 41.8% | SHORT (45.6%) | Variable |
| Paradex | 483 | 37.9% | LONG (39.5%) | 0%/0.02% |

**Best overall**: Lighter (zero fees, highest volume, SHORT bias works)

---

## The Winning Strategy (v9-qwen-enhanced)

**Alpha Arena Winner**: +22.3% return in 17 days

### 5-Signal Scoring System
1. RSI Signal (0-1 points)
2. MACD Signal (0-1 points)
3. Volume Signal (0-1 points)
4. Price Action Signal (0-1 points)
5. OI + Price Confluence (0-1 points)

### Trading Rules
- Score < 2.5 → NO_TRADE
- Score 2.5-3.0 → Tier 1 only (BTC, ETH, SOL)
- Score 3.0-4.0 → Standard trade
- Score > 4.0 → High conviction

### Funding Rate Zones
- Extreme Positive (>+0.03%): FAVOR LONGS (short squeeze potential)
- Extreme Negative (<-0.03%): FAVOR SHORTS (long liquidation potential)

See `llm_agent/prompts_archive/v9_qwen_enhanced.txt` for full prompt.

---

## Project Structure

```
hopium-agents/
├── hibachi_agent/           # Hibachi exchange adapter
├── lighter_agent/           # Lighter exchange adapter
├── extended_agent/          # Extended Lighter adapter (Python 3.11+)
├── paradex_agent/           # Paradex Grid MM adapter
├── llm_agent/               # Shared strategy engine
│   ├── llm/                 # LLM client & prompt formatting
│   ├── data/                # Market data & indicators
│   ├── prompts_archive/     # Historical prompts (v1-v17+)
│   └── self_learning.py     # Win/loss tracking, symbol blocking
├── dexes/                   # Exchange SDK wrappers
├── research/                # Research and experiments
├── scripts/                 # Utility scripts
├── logs/                    # Trade logs (gitignored)
├── MASTER_ANALYSIS.md       # Comprehensive project analysis
├── CLAUDE.md                # This file
├── CONTRIBUTING.md          # Contribution guide
└── README.md                # Project overview
```

---

## Development Guidelines

**IMPORTANT**:
- Never provide time estimates in responses
- Focus on what needs to be done, not how long it might take

**CRITICAL - P&L AND ACCOUNT DATA**:
- NEVER trust dashboard scripts, log aggregations, or local tracking for P&L
- ALWAYS query the exchange API directly for balance, positions, and P&L
- Local tracking has been wrong repeatedly - the exchange is the source of truth
- When reporting account status, ALWAYS call the exchange SDK first:
```python
# Hibachi example - ALWAYS use this pattern
from dexes.hibachi.hibachi_sdk import HibachiSDK
sdk = HibachiSDK(api_key, api_secret, account_id)
balance = await sdk.get_balance()  # Real balance
positions = await sdk.get_positions()  # Real positions
```
- NEVER say "+$X profit" without first querying the exchange
- If a dashboard or script shows P&L, VERIFY IT against exchange data

**CRITICAL - Blockchain Addresses**:
- NEVER truncate or abbreviate addresses (e.g., "0x023a...")
- ALWAYS use complete, full-length addresses when querying APIs
- Partial addresses return incorrect/empty data

---

## Mandatory Documentation Updates

**LEARNINGS.md** - Update when:
- A strategy parameter change is tested (document results)
- A bug is discovered and fixed (document root cause)
- Qwen consultation provides new insights
- A hypothesis is proven or disproven
- Exchange-specific behavior is discovered

**Required fields for each learning**:
- Date
- Evidence (log file, date range, specific data)
- Root cause or explanation
- Resolution or recommendation
- Knowledge gaps identified

**PROGRESS.md** - Update when:
- Bot configuration changes (parameters, strategy)
- Bot is started, stopped, or switched
- Exchange accounts change
- New integrations go live

**Rule**: If you learned something significant, it MUST be documented before the session ends.

---

## Consulting Qwen (LLM API)

For strategy questions, use Qwen via OpenRouter:

```python
import requests
import os
from dotenv import load_dotenv
load_dotenv('.env')

api_key = os.getenv('OPEN_ROUTER')
response = requests.post(
    "https://openrouter.ai/api/v1/chat/completions",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json={
        "model": "qwen/qwen-2.5-72b-instruct",
        "messages": [
            {"role": "system", "content": "You are an expert quant trader."},
            {"role": "user", "content": "YOUR QUESTION HERE"}
        ],
        "max_tokens": 800
    }
)
print(response.json()['choices'][0]['message']['content'])
```

**Qwen Consultation Pattern** (from MASTER_ANALYSIS.md):
1. Provide FULL historical context (not just recent)
2. Include specific numbers (WR, P&L, trade counts)
3. Ask specific questions, not open-ended
4. Request direct, implementable recommendations

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `logs/strategy_switches.log` | Strategy evolution history |
| `logs/trades/*.json` | All trade records |
| `llm_agent/prompts_archive/v9_qwen_enhanced.txt` | Winning strategy prompt |
| `logs/strategies/self_improving_llm_state.json` | Self-learning state |
| `research/strategies/GRID_MM_EVOLUTION.md` | Grid MM history |

---

## Agent Commands

```bash
# Check running agents
ps aux | grep -E "(hibachi|lighter|extended|paradex)_agent"

# View logs
tail -f logs/hibachi_bot.log
tail -f logs/lighter_bot.log

# Stop an agent
pkill -f "hibachi_agent.bot_hibachi"

# Start agents
nohup python3 -u -m hibachi_agent.bot_hibachi --live --interval 600 > logs/hibachi_bot.log 2>&1 &
nohup python3 -u -m lighter_agent.bot_lighter --live --interval 300 > logs/lighter_bot.log 2>&1 &
nohup python3.11 -u -m extended_agent.bot_extended --live --strategy C --interval 300 > logs/extended_bot.log 2>&1 &
nohup python3 scripts/grid_mm_live.py > logs/grid_mm_live.log 2>&1 &
```

---

## Git Practices

**What to Commit**:
- Source code (`.py`)
- Documentation (`.md`)
- Configuration templates (`.env.example`)
- Requirements (`requirements.txt`)

**What NOT to Commit**:
- `.env` (secrets)
- `logs/` (trade data)
- `__pycache__/`
- IDE files

---

## Data Sources & APIs

### Lighter DEX
- **Base URL**: `https://api.lighter.xyz`
- **Docs**: `https://apidocs.lighter.xyz`
- **Fees**: ZERO (maker and taker)
- **Keys**: `LIGHTER_PUBLIC_KEY`, `LIGHTER_PRIVATE_KEY` in `.env`

### Hibachi DEX
- Alpha Arena venue
- Used for v9-qwen-enhanced deployment

### Paradex
- **Fees**: 0% maker (rebates!), 0.02% taker
- **Min Notional**: $10 per order
- Best for Grid market making

### Cambrian API (Deep42)
- **Base URL**: `https://opabinia.cambrian.network/api/v1`
- **Key**: `CAMBRIAN_API_KEY` in `.env`
- **Note**: Good for entry signals, NOT for exit decisions

---

## Task Master AI Instructions

Import Task Master's development workflow commands and guidelines:
@./.taskmaster/CLAUDE.md
