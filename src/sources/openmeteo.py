"""Open-Meteo Previous Runs API → 표준 long-format."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests

from src.schema import (
    SOURCE_GROUND_TRUTH,
    SOURCE_OPENMETEO_ECMWF,
    STATION_SEOUL,
    STANDARD_COLUMNS,
    VARIABLE_TEMPERATURE,
)

PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
SEOUL_LAT = 37.5665
SEOUL_LON = 126.9780
ECMWF_MODEL = "ecmwf_ifs"
PAST_DAYS = 14

# API 응답 키 → (lead_time_h, source)
_FORECAST_MAP: tuple[tuple[str, int, str], ...] = (
    ("temperature_2m_previous_day1", 24, SOURCE_OPENMETEO_ECMWF),
    ("temperature_2m_previous_day2", 48, SOURCE_OPENMETEO_ECMWF),
)
# previous_day0 는 응답에서 temperature_2m 으로 내려온다.
_GROUND_TRUTH_KEY = "temperature_2m"


class OpenMeteoFetchError(RuntimeError):
    """Previous Runs API 호출 실패."""


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(STANDARD_COLUMNS))


def _hourly_to_long(
    hourly: dict[str, Any],
    *,
    station: str,
    variable: str,
) -> pd.DataFrame:
    """Open-Meteo hourly 블록을 표준 long-format 으로 변환."""
    times = hourly.get("time", [])
    if not times:
        return _empty_frame()

    valid_time = pd.to_datetime(times, utc=True)
    rows: list[dict[str, Any]] = []

    # 정답 프록시 (previous_day0 → temperature_2m)
    truth_vals = hourly.get(_GROUND_TRUTH_KEY, [])
    for vt, val in zip(valid_time, truth_vals, strict=False):
        if val is None or (isinstance(val, float) and math.isnan(val)):
            continue
        rows.append(
            {
                "station": station,
                "valid_time": vt,
                "lead_time_h": 0,
                "variable": variable,
                "value": float(val),
                "source": SOURCE_GROUND_TRUTH,
            }
        )

    for api_key, lead_h, source in _FORECAST_MAP:
        fcst_vals = hourly.get(api_key, [])
        for vt, val in zip(valid_time, fcst_vals, strict=False):
            if val is None or (isinstance(val, float) and math.isnan(val)):
                continue
            rows.append(
                {
                    "station": station,
                    "valid_time": vt,
                    "lead_time_h": lead_h,
                    "variable": variable,
                    "value": float(val),
                    "source": source,
                }
            )

    if not rows:
        return _empty_frame()
    return pd.DataFrame(rows, columns=list(STANDARD_COLUMNS))


def fetch_seoul_temperature_slice(
    *,
    past_days: int = PAST_DAYS,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """서울 기온 슬라이스 — 라이브 Previous Runs API."""
    params = {
        "latitude": SEOUL_LAT,
        "longitude": SEOUL_LON,
        "hourly": ",".join(
            [
                "temperature_2m_previous_day0",
                "temperature_2m_previous_day1",
                "temperature_2m_previous_day2",
            ]
        ),
        "models": ECMWF_MODEL,
        "past_days": past_days,
        "timezone": "UTC",
    }
    sess = session or requests.Session()
    resp = sess.get(PREVIOUS_RUNS_URL, params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    hourly = payload.get("hourly")
    if not hourly:
        raise OpenMeteoFetchError("hourly 데이터가 없습니다.")

    df = _hourly_to_long(hourly, station=STATION_SEOUL, variable=VARIABLE_TEMPERATURE)
    if df.empty:
        raise OpenMeteoFetchError("유효한 기온 행이 없습니다.")
    return df


def make_synthetic_seoul_temperature_slice(
    *,
    past_days: int = PAST_DAYS,
    end_time: datetime | None = None,
) -> pd.DataFrame:
    """라이브 실패 시 fallback — 동일 스키마의 결정론적 합성 데이터."""
    end = end_time or datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=past_days)
    hours = pd.date_range(start, end, freq="h", inclusive="left", tz="UTC")

    rows: list[dict[str, Any]] = []
    for vt in hours:
        hour = vt.hour
        base = 12.0 + 6.0 * math.sin((hour - 6) * math.pi / 12.0)
        truth = base + 0.3 * math.sin(hour * 0.5)
        rows.append(
            {
                "station": STATION_SEOUL,
                "valid_time": vt,
                "lead_time_h": 0,
                "variable": VARIABLE_TEMPERATURE,
                "value": truth,
                "source": SOURCE_GROUND_TRUTH,
            }
        )
        for lead_h, bias in ((24, 0.8), (48, 1.4)):
            forecast = truth + bias + 0.2 * math.cos(hour * 0.3 + lead_h)
            rows.append(
                {
                    "station": STATION_SEOUL,
                    "valid_time": vt,
                    "lead_time_h": lead_h,
                    "variable": VARIABLE_TEMPERATURE,
                    "value": forecast,
                    "source": SOURCE_OPENMETEO_ECMWF,
                }
            )

    return pd.DataFrame(rows, columns=list(STANDARD_COLUMNS))
