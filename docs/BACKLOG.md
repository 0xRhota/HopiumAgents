# Backlog — ideas + data sources to potentially evaluate later

Things we've looked at and parked. Check before adding something new to avoid re-evaluating.

---

## Massive.com data API (evaluated 2026-04-21, deferred)

**API key stored** in `.env` as `MASSIVE_API_KEY` for future use.

### What Massive offers (crypto)
- Spot CEX OHLC bars (custom intervals, daily, previous day)
- Trades tape (last trade, trades over range)
- Snapshots (full market, single ticker, top movers, unified)
- Server-side TA (RSI, MACD, EMA, SMA)
- Real-time WebSocket: trades, quotes, minute/second OHLC, Fair Market Value feed (aggregated across CEX)

### Why deferred
Spot-CEX-only data provider. Missing the perp signals we actually trade on:
- ❌ Funding rates
- ❌ Open interest
- ❌ Liquidations
- ❌ L2 order book depth
- ❌ Perpetuals-specific data
- ❌ Macro (DXY, fear/greed)

### Possible future use cases (low-to-medium ROI)
1. **Basis-divergence signal** — compare Massive's aggregated CEX spot Fair Market Value to our perp prices. >30 bps deviation could be a mean-reversion trigger.
2. **Cross-venue volume attribution** — if the trade tape tags which CEX filled, we could detect venue concentration shifts (Coinbase buy pressure surge preceding BTC pumps, etc.).
3. **MCP integration** (`github.com/massive-com/mcp_massive`) for ad-hoc research queries. Not for the trading loop.
4. **Redundant TA sanity check** — their multi-venue aggregated indicators vs our single-exchange Binance kline calcs. Marginal.

### Better alternatives for the perp-specific gaps
If we want to close any of the missing-signal gaps, these are higher-leverage:
- **Coinglass** — funding, OI, liquidations across venues. Free tier.
- **Binance/Bybit direct REST** — free, accurate for majors.
- **Kaiko free tier** — CEX spot.

### Docs
- REST llms.txt: https://massive.com/docs/rest/llms.txt
- WebSocket llms.txt: https://massive.com/docs/websocket/llms.txt
- MCP server: https://github.com/massive-com/mcp_massive
