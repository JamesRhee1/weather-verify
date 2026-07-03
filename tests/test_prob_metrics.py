"""확률예보 검증 지표 단위 테스트 (손계산 fixture)."""

from __future__ import annotations

import math

import pandas as pd
import pytest
from src.core.metrics import brier_score, brier_skill_score, reliability_table

# --- brier_score ---


def test_brier_score_perfect_forecast_is_zero():
    # (1-1)^2 + (0-0)^2 = 0
    df = pd.DataFrame({"prob": [1.0, 0.0, 1.0, 0.0], "rain": [1, 0, 1, 0]})
    assert brier_score(df, "prob", "rain") == pytest.approx(0.0)


def test_brier_score_hand_calc_two_points():
    # p=[0.2, 0.8], o=[0, 1] → (0.2^2 + 0.2^2) / 2 = 0.04
    df = pd.DataFrame({"prob": [0.2, 0.8], "rain": [0, 1]})
    assert brier_score(df, "prob", "rain") == pytest.approx(0.04)


def test_brier_score_constant_forecast_half_rain():
    # p=0.5, o=[1,1] → (0.5-1)^2 = 0.25
    df = pd.DataFrame({"prob": [0.5, 0.5], "rain": [1, 1]})
    assert brier_score(df, "prob", "rain") == pytest.approx(0.25)


def test_brier_score_ignores_nan_rows():
    # 유효 1행: (0.3-1)^2 = 0.49
    df = pd.DataFrame({"prob": [0.3, float("nan")], "rain": [1, 0]})
    assert brier_score(df, "prob", "rain") == pytest.approx(0.49)


def test_brier_score_empty_returns_nan():
    df = pd.DataFrame({"prob": [float("nan")], "rain": [1]})
    assert math.isnan(brier_score(df, "prob", "rain"))


def test_brier_score_missing_column_raises():
    df = pd.DataFrame({"prob": [0.5]})
    with pytest.raises(KeyError, match="rain"):
        brier_score(df, "prob", "rain")


# --- brier_skill_score ---


def test_brier_skill_score_perfect_beats_climatology():
    # o=[1,0,0,0] clim=0.25, BS=0 → BSS=1
    df = pd.DataFrame({"prob": [1.0, 0.0, 0.0, 0.0], "rain": [1, 0, 0, 0]})
    assert brier_skill_score(df, "prob", "rain") == pytest.approx(1.0)


def test_brier_skill_score_climatology_forecast_is_zero():
    # 항상 p=mean(o)=0.25 → BS = BS_clim → BSS=0
    df = pd.DataFrame({"prob": [0.25, 0.25, 0.25, 0.25], "rain": [1, 0, 0, 0]})
    assert brier_skill_score(df, "prob", "rain") == pytest.approx(0.0)


def test_brier_skill_score_hand_calc():
    # o=[1,0], clim=0.5, BS_clim = ((0.5-1)^2+(0.5-0)^2)/2 = 0.25
    # p=[0.8,0.2], BS = ((0.8-1)^2+(0.2-0)^2)/2 = (0.04+0.04)/2 = 0.04
    # BSS = 1 - 0.04/0.25 = 0.84
    df = pd.DataFrame({"prob": [0.8, 0.2], "rain": [1, 0]})
    assert brier_skill_score(df, "prob", "rain") == pytest.approx(0.84)


def test_brier_skill_score_all_same_outcome_returns_nan():
    df = pd.DataFrame({"prob": [0.3, 0.7], "rain": [1, 1]})
    assert math.isnan(brier_skill_score(df, "prob", "rain"))


def test_brier_skill_score_negative_when_worse_than_climatology():
    # o=[1,0,0,0] clim=0.25, p=[0,1,1,1] → BS > BS_clim
    df = pd.DataFrame({"prob": [0.0, 1.0, 1.0, 1.0], "rain": [1, 0, 0, 0]})
    assert brier_skill_score(df, "prob", "rain") < 0.0


# --- reliability_table ---


def test_reliability_table_two_bins_hand_calc():
    # bin [0,0.5]: prob 0.2,0.3 → forecast_mean=0.25, rain [0,1] → obs=0.5, n=2
    # bin (0.5,1]: prob 0.8 → forecast_mean=0.8, rain [1] → obs=1.0, n=1
    df = pd.DataFrame({"prob": [0.2, 0.3, 0.8], "rain": [0, 1, 1]})
    table = reliability_table(df, "prob", "rain", n_bins=2)
    assert len(table) == 2
    low = table.iloc[0]
    assert low["forecast_mean"] == pytest.approx(0.25)
    assert low["observed_freq"] == pytest.approx(0.5)
    assert low["n"] == 2
    high = table.iloc[1]
    assert high["forecast_mean"] == pytest.approx(0.8)
    assert high["observed_freq"] == pytest.approx(1.0)
    assert high["n"] == 1


def test_reliability_table_decile_bins():
    # prob 0.05 → [0,0.1], 0.15 → (0.1,0.2]
    df = pd.DataFrame({"prob": [0.05, 0.15], "rain": [0, 1]})
    table = reliability_table(df, "prob", "rain", n_bins=10)
    assert len(table) == 2
    assert table.iloc[0]["bin_upper"] == pytest.approx(0.1)
    assert table.iloc[1]["bin_lower"] == pytest.approx(0.1)


def test_reliability_table_pop_percent_converted_at_call_site():
    # KMA POP 30%, 70% → prob 0.3, 0.7 after /100
    pop_pct = pd.Series([30.0, 70.0])
    prob = pop_pct / 100.0
    df = pd.DataFrame({"prob": prob, "rain": [0, 1]})
    table = reliability_table(df, "prob", "rain", n_bins=10)
    assert len(table) == 2
    assert table["n"].sum() == 2


def test_reliability_table_empty_input():
    df = pd.DataFrame({"prob": [float("nan")], "rain": [1]})
    table = reliability_table(df, "prob", "rain")
    assert table.empty


def test_reliability_table_invalid_n_bins():
    df = pd.DataFrame({"prob": [0.5], "rain": [1]})
    with pytest.raises(ValueError, match="n_bins"):
        reliability_table(df, "prob", "rain", n_bins=0)
