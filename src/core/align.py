"""예보·정답 정렬 — 순수 pandas, 외부 의존성 0."""
from __future__ import annotations

import pandas as pd

from src.schema import STANDARD_COLUMNS, SOURCE_GROUND_TRUTH

_REQUIRED_JOIN_KEYS = ("station", "valid_time", "variable")


def _validate_standard(df: pd.DataFrame, name: str) -> None:
    missing = [c for c in STANDARD_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{name}: 표준 스키마 컬럼 누락 {missing}")
        raise TypeError(f"{name}: valid_time 은 datetime 이어야 합니다.")
    if df["valid_time"].dt.tz is None:
        raise ValueError(f"{name}: valid_time 은 timezone-aware UTC 여야 합니다.")


def align_forecasts_to_truth(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """여러 source long-format DataFrame 을 정답과 join 해 wide-format 반환.

    Returns:
        station, valid_time, variable, lead_time_h, forecast_value, truth_value, source
    """
    if not frames:
        raise ValueError("frames 가 비어 있습니다.")

    validated = []
    for i, frame in enumerate(frames):
        _validate_standard(frame, f"frames[{i}]")
        df = frame[list(STANDARD_COLUMNS)].copy()
        validated.append(df)

    combined = pd.concat(validated, ignore_index=True)
    truth = combined[combined["source"] == SOURCE_GROUND_TRUTH].copy()
    forecasts = combined[combined["source"] != SOURCE_GROUND_TRUTH].copy()

    if truth.empty:
        raise ValueError("ground_truth 행이 없습니다.")
    if forecasts.empty:
        raise ValueError("예보 행이 없습니다.")

    truth_keyed = (
        truth.groupby(list(_REQUIRED_JOIN_KEYS), as_index=False)["value"]
        .first()
        .rename(columns={"value": "truth_value"})
    )

    aligned = forecasts.merge(truth_keyed, on=list(_REQUIRED_JOIN_KEYS), how="inner")
    aligned = aligned.rename(columns={"value": "forecast_value"})
    aligned = aligned.dropna(subset=["forecast_value", "truth_value"])

    return aligned[
        [
            "station",
            "valid_time",
            "variable",
            "lead_time_h",
            "forecast_value",
            "truth_value",
            "source",
        ]
    ].sort_values(["lead_time_h", "valid_time"])
