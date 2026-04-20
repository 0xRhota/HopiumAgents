import pandas as pd
from core.backtest.grid_search import grid_search


def test_one_row_per_param_combo():
    df = grid_search(
        bars=pd.DataFrame({"close": [1.0]}),
        param_grid=[{"x": 1}, {"x": 2}, {"x": 3}],
        runner=lambda b, p: {"net_pnl": p["x"] * 0.1, "trades": 5, "wr": 0.4},
    )
    assert len(df) == 3
    assert "x" in df.columns and "net_pnl" in df.columns


def test_sorted_by_score_desc():
    df = grid_search(
        bars=pd.DataFrame({"close": [1.0]}),
        param_grid=[{"x": i} for i in [1, 5, 3, 2, 4]],
        runner=lambda b, p: {"net_pnl": p["x"]},
        sort_by="net_pnl",
    )
    assert list(df["net_pnl"]) == [5, 4, 3, 2, 1]
