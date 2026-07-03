"""표준 long-format 스키마 정의.

모든 source 모듈은 아래 컬럼만 갖는 DataFrame을 반환해야 한다.

컬럼:
    station       지점 ID (예: "seoul")
    valid_time    예보 유효시각 (UTC, timezone-aware)
    lead_time_h   리드타임 시간 단위 정수 (예: 24, 48; 정답 프록시는 0)
    variable      변수명 (예: "temperature_2m")
    value         float
    source        출처 (예: "openmeteo_ecmwf", "ground_truth")

정답값(ground truth)은 v0에서 Open-Meteo previous_day0 프록시이며,
추후 ASOS 실측으로 교체할 때도 동일 스키마·source 라벨 규칙을 유지한다.
"""
from __future__ import annotations

STANDARD_COLUMNS: tuple[str, ...] = (
    "station",
    "valid_time",
    "lead_time_h",
    "variable",
    "value",
    "source",
)

VARIABLE_TEMPERATURE = "temperature_2m"
STATION_SEOUL = "seoul"
SOURCE_GROUND_TRUTH = "ground_truth"
SOURCE_OPENMETEO_ECMWF = "openmeteo_ecmwf"
