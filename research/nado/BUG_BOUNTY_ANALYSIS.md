# Nado Bug Bounty Analysis

**Date**: 2026-02-16 (updated)
**Program**: https://hackenproof.com/nado (Nado Smart Contracts)
**Scope**: https://github.com/nadohq/nado-contracts (Solidity)
**Rewards**: $50 (Low) — $500,000 (Critical)
**Status**: Active, 144 hackers, $13,800 total rewards paid, 357 submissions

---

## Program Scope

**In scope**: Smart contract bugs causing:
- Stealing or loss of funds
- Unauthorized transactions
- Transaction manipulation
- Logic bugs (behavior differs from business description)
- Reentrancy, reordering, over/underflows

**Out of scope**: Theoretical bugs without PoC, gas optimizations, front-running-only attacks, compiler issues

**Requirements**: Must include runnable PoC (AI-generated reports without PoC rejected)

---

## Contract Architecture Summary

**Chain**: Ink L2 (EVM, chain ID 57073)
**Solidity**: 0.8.13, OpenZeppelin upgradeable, prb-math fixed-point
**Origin**: Vertex Protocol fork (confirmed: `EIP712_init("Nado", "0.0.1")`, `VRTX_PRODUCT_ID = 41`)

**Key contracts** (19 total):

| Contract | Role | Security Level |
|----------|------|----------------|
| `Clearinghouse.sol` | Central hub, fund management, health tracking | CRITICAL |
| `ClearinghouseLiq.sol` | Liquidation logic, margin enforcement | CRITICAL |
| `Endpoint.sol` | Entry point, EIP-712 signature verification, slow-mode | CRITICAL |
| `OffchainExchange.sol` | Order matching, fee distribution | CRITICAL |
| `PerpEngine.sol` | Perp positions, funding rates, PnL settlement | CRITICAL |
| `SpotEngine.sol` | Spot balances, interest accrual, lending | CRITICAL |
| `Verifier.sol` | Schnorr signature verification | HIGH |
| `WithdrawPool.sol` | Fast/slow withdrawal processing | HIGH |

**Architecture**: Off-chain sequencer matches orders → submits to Endpoint → Clearinghouse routes to engines → health checks after state updates.

---

## Our Account Fee Rates (Verified via API)

Queried `fee_rates` endpoint for our subaccount:

| Parameter | Value | Notes |
|-----------|-------|-------|
| **Taker fee** | 3.5 bps (0.035%) | Entry Tier (all products) |
| **Maker fee** | 1.0 bps (0.01%) | Entry Tier (positive = we pay) |
| **Taker sequencer fee** | $0.00 | No sequencer surcharge |
| **Liquidation sequencer fee** | $1.00 | Per liquidation |
| **Min notional** | $100 | All products |

**Documented fee tiers** (from docs.nado.xyz/fees-and-rebates):
- Entry ($0 vol): Taker 3.5 bps, Maker 1.0 bps
- Mid ($25M vol): Taker 3.0 bps, Maker 0.5 bps
- Elite ($5B+ vol): Taker 1.5 bps, Maker -0.8 bps (rebate)

---

## Archive API Verification (500 matches analyzed)

### Aggregate Stats
- Total matches: 500 (429 maker, 71 taker)
- Total realized PnL: +$15.76
- Total fees paid: $7.99
- Net result: +$7.77
- Products traded: BTC-PERP (pid 2), ETH-PERP (pid 4)

### FINDING 1: PnL Settlement — DEBUNKED (Working Correctly)

**Original concern**: 55 of 60 BTC trades logged `pnl_delta: 0.0`

**Root cause**: Our bot was reading `realized_pnl` from the maker fill (entry), which is always 0 by design. The taker fill (exit/close) contains the correct `realized_pnl` value.

**Archive API confirmation**:
- Maker fills: `realized_pnl = 0` (correct — no PnL at entry)
- Taker fills: `realized_pnl` = actual gain/loss (e.g., +$2.14, -$1.03, etc.)

**Verdict**: NOT a bug. Our local tracking was reading the wrong fill.

---

### FINDING 2: v_quote_balance Semantics — STILL VALID

**Severity**: Medium (logic bug / misleading data)

The `v_quote_balance` field stores **notional position debt**, not unrealized PnL:

| State | Spot Balance | v_quote_balance | Naive Equity |
|-------|-------------|-----------------|-------------|
| Flat | $81.94 | $0 | $81.94 (correct) |
| LONG $105 BTC | $81.94 | -$102.65 | -$20.71 (WRONG) |
| SHORT $105 BTC | $81.94 | +$101.04 | $182.98 (WRONG) |

**Risk**: Any third-party integration using `spot_balance + v_quote_balance` as equity would make catastrophically wrong risk decisions. The correct formula requires mark-to-market of the position using oracle price.

**Status**: Need to check if Nado docs clearly explain this or if the field name suggests unrealized PnL.

---

### FINDING 3: POST_ONLY vs Oracle Price Mismatch — CONFIRMED

**Severity**: Low-Medium (logic inconsistency)

16+ POST_ONLY rejections (error 2008 "crosses the book") even with limit price set 8+ bps from oracle price. The contract validates against the actual order book bid/ask, but the public API only exposes oracle price.

**Impact**: API users cannot reliably place POST_ONLY orders on low-liquidity markets. This is a UX/API gap rather than a smart contract bug.

---

### FINDING 4: Fee Overcharge on POST_ONLY — DEBUNKED

**Original concern**: Maker fees charged on POST_ONLY entries

**Archive API confirmation**: Our account's maker fee is +1.0 bps (Entry tier). This is the documented rate — Nado does NOT advertise 0% maker fees at Entry tier. Our original assumption was wrong.

**Fee tier documentation** confirms: Entry tier makers pay 1 bps. Only Elite tier ($5B+ volume) gets -0.8 bps maker rebate.

**Verdict**: NOT a bug. We were on Entry tier paying documented rates.

---

### FINDING 5: Minimum Taker Fee Floor — DOCUMENTED BEHAVIOR

**Original concern**: ETH taker fills showed flat $0.035 fee regardless of notional size ($18-$51 trades).

**Explanation** (from docs.nado.xyz/fees-and-rebates):
> "Orders < minimum size: Minimum fee applies: `minSize × feeRate` (effectively treating small orders as if they were minSize)"

With minSize = $100 and taker rate = 0.035%:
- $100 × 0.035% = $0.035 minimum fee
- All ETH trades below $100 notional charged exactly $0.035

**Contract code** confirms in `OffchainExchange.sol::applyFee()`:
```solidity
if (taker && alreadyMatched == 0) {
    meteredQuote += market.minSize;  // Always fee the minSize amount first
}
```

**Verdict**: NOT a bug — documented and intentional.

---

### FINDING 6: Archive Fee Rate Discrepancy — INCONCLUSIVE

Some Archive API matches show fee rates inconsistent with our current tier:
- Maker fills at -0.008% (Elite tier rebate) despite Entry tier assignment
- Taker fills at 0.015% (Elite tier rate) despite 3.5 bps assignment

Possible explanations:
1. Fee tier changed during promotional/alpha period
2. Counterparty fee rate leaking into match data
3. Builder fee adjustment affecting displayed rate

**Status**: Needs more investigation but likely explained by tier changes during alpha.

---

## Smart Contract Audit Observations (from code review)

### Fee Calculation: `applyFee()` in OffchainExchange.sol

**Mechanism**:
1. Taker: `meteredQuote = max(quoteAmount, minSize)` → `fee = meteredQuote * takerRate`
2. Maker: `fee = quoteAmount * makerRate` (no minimum)
3. Builder fees: Additional `|matchQuote| * builderFeeRate` from order appendix
4. `X_ACCOUNT` pays zero fees (protocol account)

**Builder fee validation**: Builder rates must be within `[lowestFeeRate, highestFeeRate]` set per builder.

**Potential attack surface**: `makerAccruesTakerFee()` — if maker is zero-address AND has maker rate ≤ -3 bps, maker receives the TAKER's fee. Could this be exploited?

### Oracle Price Centralization (HIGH)
- Prices set by sequencer via `UpdatePrice` transaction
- No staleness check, no price bounds, no circuit breaker
- Sequencer could submit manipulated prices to trigger unfair liquidations
- **Attack**: Submit BTC price = $1,000 → all longs instantly liquidated → insurance drained
- **Note**: This is a Vertex-inherited design. Likely won't qualify as "bug" since it's an acknowledged trust assumption.

### Fixed Liquidation Fee (MEDIUM)
- `LIQUIDATION_FEE = 1e18` ($1 flat regardless of position size)
- A $1M position pays same $1 fee as a $100 position
- Could incentivize mass liquidation griefing at low cost

### Slow Mode Censorship (MEDIUM)
- `SLOW_MODE_TX_DELAY = 3 days`
- User submits withdrawal → sequencer can delay up to 3 days
- During delay: market moves, position gets liquidated, funds gone
- No emergency escape mechanism

### ManualAssert Admin Override (MEDIUM)
- Owner can call `ManualAssert()` to override state
- Could inflate/deflate interest multipliers
- Requires owner key but high-impact if compromised

### Schnorr Signature Implementation (NEEDS AUDIT)
- Custom non-standard Schnorr verification in `Verifier.sol`
- Supports up to 8 aggregated signers
- Non-standard crypto implementations are high-risk
- Inherited from Vertex — may have already been audited there

---

## Remaining Investigation Leads

### 1. Builder Fee Manipulation (MEDIUM PRIORITY)
The order appendix encodes a 10-bit builder fee rate. Could a malicious builder set extreme rates? Validation exists but edge cases (overflow, underflow in int128) need review.

### 2. `makerAccruesTakerFee` Exploitation (HIGH PRIORITY)
If a maker order from zero-address AND with maker rate ≤ -3bps matches a taker, the maker RECEIVES the taker's fee. Questions:
- Can anyone create orders from zero-address?
- What controls the per-account maker rate assignment?
- Could a malicious actor get assigned a deeply negative maker rate?

### 3. Price Increment Rounding in x18 (LOW PRIORITY)
Float-to-x18 conversion can produce values not aligned to `price_increment_x18`. We hit this ourselves (WLFI order rejected). The SDK now snaps to increments, but the contract could silently accept slightly off-increment prices in some edge cases.

### 4. Cross-Product Liquidation Cascade (MEDIUM PRIORITY)
With unified cross-margin, liquidation on one product can cascade to all positions. If an attacker can manipulate the oracle price of a low-liquidity product, they could trigger liquidations on a user's BTC/ETH positions.

---

## Summary Assessment

| Finding | Status | Bug Bounty Eligible? |
|---------|--------|---------------------|
| PnL settlement $0 | DEBUNKED | No |
| v_quote_balance semantics | Needs more investigation | Possibly (logic bug) |
| POST_ONLY oracle mismatch | Confirmed but API gap | Unlikely (UX issue) |
| Fee overcharge | DEBUNKED | No |
| Minimum taker fee | Documented behavior | No |
| Fee rate discrepancy | Inconclusive | Needs data |
| Oracle centralization | Design choice | Unlikely |
| makerAccruesTakerFee | Needs deeper review | Possibly (if exploitable) |
| Builder fee edge cases | Needs review | Possibly |

**Bottom line**: Our bot interactions haven't surfaced a clear, exploitable smart contract bug with PoC potential. The strongest remaining leads are:
1. `makerAccruesTakerFee` exploitation path
2. Cross-product liquidation via low-liquidity oracle manipulation
3. Builder fee validation edge cases

All three require deep Solidity analysis with Foundry/Hardhat PoC — beyond what we can demonstrate from API trading alone.

---

## Action Items

1. ~~Query Nado Archive API for actual per-trade PnL and fees~~ ✅ DONE
2. **Read PerpEngine.sol** to understand vQuoteBalance semantics and verify Finding 2
3. **Audit `makerAccruesTakerFee()` logic** for exploitation paths
4. **Review builder fee validation** for overflow/underflow edge cases
5. **Write PoC** for any confirmed bugs (required for submission)
6. **Register on HackenProof** if submitting
7. **Do NOT disclose** any findings publicly (per program rules)

---

## Files With Evidence

| File | Contents |
|------|----------|
| `logs/momentum/nado_trades.jsonl` | Trade records with PnL data |
| `logs/momentum/nado_audit.csv` | Equity snapshots |
| `logs/momentum/nado_hourly.jsonl` | Hourly equity oscillations |
| `logs/momentum/nado_bot.log` | Bot log with POST_ONLY rejections |
| `logs/momentum/nado_kbonk_bot.log` | kBONK bot log |
| `logs/momentum/nado_wlfi_bot.log` | WLFI bot log |
| `logs/momentum/nado_doge_bot.log` | DOGE bot log |
| `dexes/nado/nado_sdk.py` | SDK with x18 conversion and signing |
| `core/strategies/momentum/exchange_adapter.py` | Adapter with equity calculation |
