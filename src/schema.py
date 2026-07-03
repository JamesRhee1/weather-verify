"""표준 long-format 스키마 정의.

모든 source 모듈은 아래 컬럼만 갖는 DataFrame을 반환해야 한다.

컬럼:
    station       지점 ID (예: "seoul")
    valid_time    예보 유효시각 (UTC, timezone-aware)
    lead_time_h   리드타임 시간 단위 정수 (예: 24, 48; 정답 프록시는 0)
    variable      변수명 (예: "temperature_2m")
    value         float
    source        출처 (예: "openmeteo_ecmwf", "ground_truth_asos")

정답값은 ASOS 실측(source=ground_truth_asos)을 우선하며,
Open-Meteo previous_day0 프록시(source=openmeteo_self_proxy)는 키·API 불가 시 대체용이다.
"""

from __future__ import annotations

import pandas as pd

STANDARD_COLUMNS: tuple[str, ...] = (
    "station",
    "valid_time",
    "lead_time_h",
    "variable",
    "value",
    "source",
)

VARIABLE_TEMPERATURE = "temperature_2m"
VARIABLE_POP = "precipitation_probability"
VARIABLE_PCP = "precipitation_amount"
STATION_SEOUL = "seoul"
SEOUL_ASOS_STN_ID = 108
SOURCE_GROUND_TRUTH_ASOS = "ground_truth_asos"
SOURCE_OPENMETEO_SELF_PROXY = "openmeteo_self_proxy"
SOURCE_OPENMETEO_ECMWF = "openmeteo_ecmwf"
SOURCE_KMA_VILAGE = "kma_vilage_fcst"

TRUTH_SOURCES = frozenset({SOURCE_GROUND_TRUTH_ASOS, SOURCE_OPENMETEO_SELF_PROXY})

# KMA API category → 표준 variable 명
KMA_CATEGORY_TO_VARIABLE: dict[str, str] = {
    "TMP": VARIABLE_TEMPERATURE,
    "POP": VARIABLE_POP,
    "PCP": VARIABLE_PCP,
}


def validate_standard_frame(df: pd.DataFrame, name: str) -> None:
    """표준 long-format DataFrame 스키마 검증."""
    missing = [c for c in STANDARD_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{name}: 표준 스키마 컬럼 누락 {missing}")
    if not pd.api.types.is_datetime64_any_dtype(df["valid_time"]):
        raise TypeError(f"{name}: valid_time 은 datetime 이어야 합니다.")
    if df["valid_time"].dt.tz is None:
        raise ValueError(f"{name}: valid_time 은 timezone-aware UTC 여야 합니다.")
