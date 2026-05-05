"""Tests for core/latency.py — latency injection."""
from __future__ import annotations

import pytest

from paper_sim.core.latency import (
    DEFAULT_PROFILES,
    LatencyInjector,
    LatencyProfile,
    get_profile,
)


class TestProfile:
    def test_validation_positive(self):
        with pytest.raises(ValueError):
            LatencyProfile("v", median_ms=0, p99_ms=100)

    def test_validation_p99_ge_median(self):
        with pytest.raises(ValueError):
            LatencyProfile("v", median_ms=100, p99_ms=50)

    def test_default_profiles_present(self):
        for v in ("paradex", "hyperliquid", "nado", "hibachi"):
            p = get_profile(v)
            assert p.median_ms > 0
            assert p.p99_ms >= p.median_ms

    def test_unknown_venue(self):
        with pytest.raises(ValueError):
            get_profile("nope")


class TestSampling:
    def test_returns_positive(self):
        inj = LatencyInjector(get_profile("paradex"), seed=42)
        for _ in range(1000):
            assert inj.sample_ms() > 0

    def test_seed_is_deterministic(self):
        a = LatencyInjector(get_profile("paradex"), seed=42)
        b = LatencyInjector(get_profile("paradex"), seed=42)
        for _ in range(50):
            assert a.sample_ms() == b.sample_ms()

    def test_median_approximation(self):
        """Sampled median should match profile.median_ms within ~10%."""
        inj = LatencyInjector(get_profile("paradex"), seed=42)
        samples = sorted(inj.sample_ms() for _ in range(2000))
        sampled_median = samples[len(samples) // 2]
        target = DEFAULT_PROFILES["paradex"].median_ms
        assert abs(sampled_median - target) / target < 0.10

    def test_p99_approximation(self):
        inj = LatencyInjector(get_profile("hyperliquid"), seed=42)
        samples = sorted(inj.sample_ms() for _ in range(5000))
        sampled_p99 = samples[int(len(samples) * 0.99)]
        target = DEFAULT_PROFILES["hyperliquid"].p99_ms
        # Allow 20% tolerance — distribution is approximate
        assert abs(sampled_p99 - target) / target < 0.20

    def test_seconds_is_ms_divided_by_1000(self):
        inj = LatencyInjector(get_profile("paradex"), seed=1)
        a = LatencyInjector(get_profile("paradex"), seed=1)
        b = LatencyInjector(get_profile("paradex"), seed=1)
        ms = a.sample_ms()
        sec = b.sample_seconds()
        assert abs(sec * 1000 - ms) < 1e-9
