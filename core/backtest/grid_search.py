"""Grid search over strategy parameters."""
from __future__ import annotations

from typing import Callable, List

import pandas as pd


def grid_search(bars: pd.DataFrame, param_grid: List[dict],
                runner: Callable[[pd.DataFrame, dict], dict],
                sort_by: str = "net_pnl") -> pd.DataFrame:
    """runner must return a dict with scalar metrics. Rows sorted desc by sort_by."""
    rows = [{**params, **runner(bars, params)} for params in param_grid]
    df = pd.DataFrame(rows)
    if sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=False).reset_index(drop=True)
    return df
