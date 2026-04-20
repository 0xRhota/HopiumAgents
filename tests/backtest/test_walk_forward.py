import pandas as pd
from core.backtest.walk_forward import walk_forward, WindowResult


def _bars(n=60):
    closes = [1.0 + 0.001 * (i % 100) for i in range(n)]
    idx = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"close": closes, "high": closes, "low": closes,
                         "open": closes, "volume": [100]*n}, index=idx)


def test_yields_one_result_per_test_window():
    bars = _bars(60)
    results = list(walk_forward(
        bars=bars, train_bars=20, test_bars=10,
        param_grid=[{"x": 1}, {"x": 2}],
        runner=lambda b, p: p["x"] * 1.0,
    ))
    assert len(results) >= 3
    for r in results:
        assert isinstance(r, WindowResult)
        assert r.best_params is not None


def test_picks_best_param_from_train():
    bars = _bars(60)
    results = list(walk_forward(
        bars=bars, train_bars=20, test_bars=10,
        param_grid=[{"x": 1}, {"x": 2}, {"x": 3}],
        runner=lambda b, p: p["x"] * 1.0,
    ))
    for r in results:
        assert r.best_params["x"] == 3
