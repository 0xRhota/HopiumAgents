"""Walk-forward analysis: rolling train/test windows for anti-overfitting."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, List

import pandas as pd


@dataclass
class WindowResult:
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_end: pd.Timestamp
    best_params: dict
    train_score: float
    test_score: float


def walk_forward(bars: pd.DataFrame, train_bars: int, test_bars: int,
                 param_grid: List[dict],
                 runner: Callable[[pd.DataFrame, dict], float]) -> Iterator[WindowResult]:
    """Slide train/test window forward. `runner(bars, params) -> score`."""
    i = 0
    while i + train_bars + test_bars <= len(bars):
        train = bars.iloc[i : i + train_bars]
        test = bars.iloc[i + train_bars : i + train_bars + test_bars]
        best_params = None
        best_score = float("-inf")
        for params in param_grid:
            s = runner(train, params)
            if s > best_score:
                best_score = s
                best_params = params
        test_score = runner(test, best_params)
        yield WindowResult(
            train_start=train.index[0], train_end=train.index[-1],
            test_end=test.index[-1], best_params=best_params,
            train_score=best_score, test_score=test_score,
        )
        i += test_bars
