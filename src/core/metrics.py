"""MAE/RMSE 등 지표 — 순수 함수, 외부 의존성 0."""
from __future__ import annotations

import pandas as pd


def compute_mae(df: pd.DataFrame, forecast_col: str, truth_col: str) -> float:
    """평균 절대 오차 (MAE). 결측 행은 제외."""
    if forecast_col not in df.columns or truth_col not in df.columns:
        raise KeyError(f"컬럼 없음: {forecast_col}, {truth_col}")
    pair = df[[forecast_col, truth_col]].dropna()
    if pair.empty:
        return float("nan")
    return float((pair[forecast_col] - pair[truth_col]).abs().mean())


def compute_rmse(df: pd.DataFrame, forecast_col: str, truth_col: str) -> float:
    """평균 제곱근 오차 (RMSE). 결측 행은 제외."""
    pair = df[[forecast_col, truth_col]].dropna()
    if pair.empty:
        return float("nan")
    diff = pair[forecast_col] - pair[truth_col]
    return float((diff**2).mean() ** 0.5)


def mae_by_lead_time(
    df: pd.DataFrame,
    forecast_col: str = "forecast_value",
    truth_col: str = "truth_value",
    lead_col: str = "lead_time_h",
) -> pd.DataFrame:
    """lead_time_h 별 MAE 를 계산해 표 형태로 반환."""
    if lead_col not in df.columns:
        raise KeyError(f"컬럼 없음: {lead_col}")

    rows: list[dict[str, float | int]] = []
    for lead, group in df.groupby(lead_col, sort=True):
        mae = compute_mae(group, forecast_col, truth_col)
        rows.append({"lead_time_h": int(lead), "mae": mae, "n": len(group.dropna(subset=[forecast_col, truth_col]))})

    return pd.DataFrame(rows)
