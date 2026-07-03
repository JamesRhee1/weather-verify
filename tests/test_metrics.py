"""metrics 모듈 합성 데이터 테스트 (네트워크 없음)."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from src.core.metrics import compute_mae, compute_rmse, mae_by_lead_time


def test_compute_mae_basic():
    df = pd.DataFrame({"forecast_value": [21.0, 22.0, 19.0], "truth_value": [20.0, 20.0, 20.0]})
    assert compute_mae(df, "forecast_value", "truth_value") == pytest.approx(4.0 / 3.0)


def test_compute_mae_ignores_nan():
    df = pd.DataFrame({"forecast_value": [21.0, float("nan")], "truth_value": [20.0, 20.0]})
    assert compute_mae(df, "forecast_value", "truth_value") == pytest.approx(1.0)


def test_compute_mae_empty_returns_nan():
    df = pd.DataFrame({"forecast_value": [float("nan")], "truth_value": [20.0]})
    assert math.isnan(compute_mae(df, "forecast_value", "truth_value"))


def test_compute_rmse_basic():
    df = pd.DataFrame({"forecast_value": [22.0, 18.0], "truth_value": [20.0, 20.0]})
    assert compute_rmse(df, "forecast_value", "truth_value") == pytest.approx(2.0)


def test_mae_by_lead_time_stratifies():
    df = pd.DataFrame(
        {
            "lead_time_h": [24, 24, 48, 48],
            "forecast_value": [21.0, 23.0, 22.0, 24.0],
            "truth_value": [20.0, 20.0, 20.0, 20.0],
        }
    )
    table = mae_by_lead_time(df)
    assert list(table["lead_time_h"]) == [24, 48]
    assert table.loc[table["lead_time_h"] == 24, "mae"].iloc[0] == pytest.approx(2.0)
    assert table.loc[table["lead_time_h"] == 48, "mae"].iloc[0] == pytest.approx(3.0)
    assert table.loc[table["lead_time_h"] == 24, "n"].iloc[0] == 2
