"""MAE/RMSE 등 지표 — 순수 함수, 외부 의존성 0."""

from __future__ import annotations

import pandas as pd


def _prob_outcome_pair(df: pd.DataFrame, prob_col: str, outcome_col: str) -> pd.DataFrame:
    if prob_col not in df.columns or outcome_col not in df.columns:
        raise KeyError(f"컬럼 없음: {prob_col}, {outcome_col}")
    return df[[prob_col, outcome_col]].dropna()


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
    if forecast_col not in df.columns or truth_col not in df.columns:
        raise KeyError(f"컬럼 없음: {forecast_col}, {truth_col}")
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

    work = df[[lead_col, forecast_col, truth_col]].copy()
    work["_abs_err"] = (work[forecast_col] - work[truth_col]).abs()
    work["_pair_ok"] = work[[forecast_col, truth_col]].notna().all(axis=1)

    result = (
        work.groupby(lead_col, sort=True)
        .agg(
            mae=("_abs_err", "mean"),
            n=("_pair_ok", "sum"),
        )
        .reset_index()
        .rename(columns={lead_col: "lead_time_h"})
    )
    result["lead_time_h"] = result["lead_time_h"].astype(int)
    result["n"] = result["n"].astype(int)
    return result


def brier_score(df: pd.DataFrame, prob_col: str, outcome_col: str) -> float:
    """Brier score — 평균 제곱 확률 오차.

    prob_col 은 0~1 확률만 허용한다. KMA POP(0~100%)은 호출부에서 /100 변환 후 전달.
    outcome_col 은 binary 실측 (0 또는 1).
    """
    pair = _prob_outcome_pair(df, prob_col, outcome_col)
    if pair.empty:
        return float("nan")
    diff = pair[prob_col] - pair[outcome_col]
    return float((diff**2).mean())


def brier_skill_score(df: pd.DataFrame, prob_col: str, outcome_col: str) -> float:
    """Brier skill score — 기준(reference)은 관측 기후 빈도(표본 평균).

    BSS = 1 - BS_fcst / BS_clim,  BS_clim 은 항상 p_clim=mean(outcome) 을 예보한 점수.
    BS_clim=0 (전부 0 또는 전부 1) 이면 nan.
    """
    pair = _prob_outcome_pair(df, prob_col, outcome_col)
    if pair.empty:
        return float("nan")

    bs = brier_score(df, prob_col, outcome_col)
    clim = float(pair[outcome_col].mean())
    bs_clim = float(((clim - pair[outcome_col]) ** 2).mean())
    if bs_clim == 0.0:
        return float("nan")
    return 1.0 - bs / bs_clim


def reliability_table(
    df: pd.DataFrame,
    prob_col: str,
    outcome_col: str,
    n_bins: int = 10,
) -> pd.DataFrame:
    """Reliability diagram 용 구간별 (예보확률 평균, 관측 빈도, n) 표.

    prob_col 은 0~1 확률. [0,1] 을 n_bins 등간격 구간으로 나눈다.
    """
    if n_bins < 1:
        raise ValueError("n_bins 는 1 이상이어야 합니다.")

    pair = _prob_outcome_pair(df, prob_col, outcome_col)
    if pair.empty:
        return pd.DataFrame(
            columns=["bin_lower", "bin_upper", "forecast_mean", "observed_freq", "n"]
        )

    edges = [i / n_bins for i in range(n_bins + 1)]
    binned = pair.copy()
    binned["_bin"] = pd.cut(
        binned[prob_col],
        bins=edges,
        include_lowest=True,
        right=True,
    )

    rows: list[dict[str, float | int]] = []
    for interval in binned["_bin"].cat.categories:
        group = binned[binned["_bin"] == interval]
        if group.empty:
            continue
        rows.append(
            {
                "bin_lower": float(interval.left),
                "bin_upper": float(interval.right),
                "forecast_mean": float(group[prob_col].mean()),
                "observed_freq": float(group[outcome_col].mean()),
                "n": int(len(group)),
            }
        )

    return pd.DataFrame(
        rows, columns=["bin_lower", "bin_upper", "forecast_mean", "observed_freq", "n"]
    )
