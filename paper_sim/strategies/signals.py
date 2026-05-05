"""Pure signal computations used by strategies.

Reimplemented minimally here to keep paper_sim isolated from
core/strategies/momentum (per the package isolation rule).
"""
from __future__ import annotations

from typing import List, Optional


def rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def macd(closes: List[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> Optional[tuple[float, float, float]]:
    if len(closes) < slow + signal:
        return None
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    if ema_fast is None or ema_slow is None:
        return None
    macd_line = ema_fast - ema_slow
    # Build MACD series for signal-line EMA
    series = []
    for i in range(slow, len(closes) + 1):
        ef = ema(closes[:i], fast)
        es = ema(closes[:i], slow)
        if ef is None or es is None:
            continue
        series.append(ef - es)
    if len(series) < signal:
        return None
    sig = ema(series[-signal * 3:] if len(series) >= signal * 3 else series, signal)
    if sig is None:
        return None
    return macd_line, sig, macd_line - sig


def atr_bps(highs: List[float], lows: List[float], closes: List[float],
            period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr = sum(trs[-period:]) / period
    last = closes[-1]
    if last <= 0:
        return None
    return atr / last * 10_000.0


def momentum_score(closes_5m: List[float], highs_5m: List[float],
                   lows_5m: List[float], volumes_5m: List[float]) -> Optional[float]:
    """5-signal score in [0, 5]: RSI + MACD + Vol + PriceAction + EMA-stack.

    Values >= 4.5 = "4-of-5 alignment" — used as score_min for tight conviction.
    """
    n = len(closes_5m)
    if n < 30:
        return None

    score = 0.0

    # RSI: extreme reversal-zone signal
    r = rsi(closes_5m, 14)
    if r is None:
        return None
    if r > 70:
        score += 1.0  # likely reversal short signal contribution
    elif r < 30:
        score += 1.0  # likely reversal long signal contribution
    else:
        score += 0.3 * (1 - abs(r - 50) / 50)

    # MACD: directional alignment
    m = macd(closes_5m)
    if m is not None:
        hist = m[2]
        score += min(1.0, abs(hist) * 100) * (1.0 if hist != 0 else 0)

    # Volume: 1x baseline, score the spike
    if len(volumes_5m) >= 20:
        avg_vol = sum(volumes_5m[-20:]) / 20
        last_vol = volumes_5m[-1]
        if avg_vol > 0:
            ratio = last_vol / avg_vol
            score += min(1.0, max(0.0, (ratio - 1.0)))

    # Price action: did the last 3 candles close in the same direction?
    last3 = closes_5m[-4:]
    if len(last3) == 4:
        diffs = [last3[i + 1] - last3[i] for i in range(3)]
        if all(d > 0 for d in diffs) or all(d < 0 for d in diffs):
            score += 1.0
        elif sum(1 for d in diffs if d > 0) >= 2 or sum(1 for d in diffs if d < 0) >= 2:
            score += 0.7

    # EMA stack: ema9 vs ema21 alignment
    e9 = ema(closes_5m[-30:], 9)
    e21 = ema(closes_5m[-30:], 21)
    if e9 is not None and e21 is not None:
        if e9 > e21 and closes_5m[-1] > e9:
            score += 1.0
        elif e9 < e21 and closes_5m[-1] < e9:
            score += 1.0
        else:
            score += 0.4

    return score


def direction_from_state(closes_5m: List[float]) -> str:
    """LONG if up-trending, SHORT if down. Used together with score."""
    if len(closes_5m) < 25:
        return "FLAT"
    e9 = ema(closes_5m[-25:], 9)
    e21 = ema(closes_5m[-25:], 21)
    if e9 is None or e21 is None:
        return "FLAT"
    if e9 > e21 and closes_5m[-1] > e9:
        return "LONG"
    if e9 < e21 and closes_5m[-1] < e9:
        return "SHORT"
    return "FLAT"
