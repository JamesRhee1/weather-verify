"""예보·정답 정렬 — 순수 pandas, 외부 의존성 0."""

from __future__ import annotations

import pandas as pd

from src.schema import STANDARD_COLUMNS, TRUTH_SOURCES, validate_standard_frame

_REQUIRED_JOIN_KEYS = ("station", "valid_time", "variable")


def align_forecasts_to_truth(
    frames: list[pd.DataFrame],
    *,
    truth_sources: frozenset[str] | None = None,
) -> pd.DataFrame:
    """여러 source long-format DataFrame 을 정답과 join 해 wide-format 반환.

    Returns:
        station, valid_time, variable, lead_time_h, forecast_value, truth_value, source
    """
    if not frames:
        raise ValueError("frames 가 비어 있습니다.")

    sources = truth_sources or TRUTH_SOURCES

    validated = []
    for i, frame in enumerate(frames):
        validate_standard_frame(frame, f"frames[{i}]")
        df = frame[list(STANDARD_COLUMNS)].copy()
        validated.append(df)

    combined = pd.concat(validated, ignore_index=True)
    truth = combined[combined["source"].isin(sources)].copy()
    forecasts = combined[~combined["source"].isin(sources)].copy()

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
