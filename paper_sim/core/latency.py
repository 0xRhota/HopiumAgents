"""Per-venue latency injector.

Models the time between strategy decision and order arriving on the venue.
Uses a log-normal-ish distribution: median + p99 calibrated from observed
live order latencies in our existing trade logs.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class LatencyProfile:
    venue: str
    median_ms: float
    p99_ms: float

    def __post_init__(self) -> None:
        if self.median_ms <= 0 or self.p99_ms <= 0:
            raise ValueError("latency must be positive")
        if self.p99_ms < self.median_ms:
            raise ValueError("p99 must be >= median")


# Calibrated from existing live bots' observed acknowledgement times.
# Keep these conservative — paper sim should err on the realistic side.
DEFAULT_PROFILES: Dict[str, LatencyProfile] = {
    "paradex": LatencyProfile("paradex", median_ms=200.0, p99_ms=800.0),
    "hyperliquid": LatencyProfile("hyperliquid", median_ms=150.0, p99_ms=600.0),
    "nado": LatencyProfile("nado", median_ms=300.0, p99_ms=1200.0),
    "hibachi": LatencyProfile("hibachi", median_ms=250.0, p99_ms=1000.0),
}


class LatencyInjector:
    """Samples per-order latency from a venue profile.

    Distribution: log-normal scaled so that median = profile.median_ms and
    99th percentile ≈ profile.p99_ms. Uses a deterministic RNG when seeded —
    important for reproducible tests.
    """

    def __init__(self, profile: LatencyProfile, seed: int | None = None):
        self.profile = profile
        self._rng = random.Random(seed)
        # Solve sigma so that p99/median = exp(z99 * sigma)
        # log(p99/median) = z99 * sigma → sigma = log(p99/median) / z99
        # z99 ≈ 2.326
        import math
        self._mu = math.log(profile.median_ms)
        ratio = profile.p99_ms / profile.median_ms
        self._sigma = math.log(ratio) / 2.326 if ratio > 1 else 0.0

    def sample_ms(self) -> float:
        """Returns latency in milliseconds, lognormal-distributed."""
        if self._sigma <= 0:
            return self.profile.median_ms
        z = self._rng.gauss(0.0, 1.0)
        import math
        return float(math.exp(self._mu + self._sigma * z))

    def sample_seconds(self) -> float:
        return self.sample_ms() / 1000.0


def get_profile(venue: str) -> LatencyProfile:
    if venue not in DEFAULT_PROFILES:
        raise ValueError(f"unknown venue: {venue}")
    return DEFAULT_PROFILES[venue]
