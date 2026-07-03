"""Open-Meteo Previous Runs API → 표준 long-format."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests

from src.schema import (
    SOURCE_OPENMETEO_ECMWF,
    SOURCE_OPENMETEO_SELF_PROXY,
    STANDARD_COLUMNS,
    STATION_SEOUL,
    VARIABLE_TEMPERATURE,
    validate_standard_frame,
)

PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
SEOUL_LAT = 37.5665
SEOUL_LON = 126.9780
ECMWF_MODEL = "ecmwf_ifs"
PAST_DAYS = 14

_FORECAST_MAP: tuple[tuple[str, int, str], ...] = (
    ("temperature_2m_previous_day1", 24, SOURCE_OPENMETEO_ECMWF),
    ("temperature_2m_previous_day2", 48, SOURCE_OPENMETEO_ECMWF),
)
_PROXY_TRUTH_KEY = "temperature_2m"


class OpenMeteoFetchError(RuntimeError):
    """Previous Runs API 호출 실패."""


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(STANDARD_COLUMNS))


def _fetch_hourly_payload(
    *,
    past_days: int = PAST_DAYS,
    session: requests.Session | None = None,
) -> dict[str, Any]:
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
    return hourly


def _proxy_truth_to_long(
    hourly: dict[str, Any],
    *,
    station: str,
    variable: str,
) -> pd.DataFrame:
    times = hourly.get("time", [])
    if not times:
        return _empty_frame()

    valid_time = pd.to_datetime(times, utc=True)
    rows: list[dict[str, Any]] = []
    truth_vals = hourly.get(_PROXY_TRUTH_KEY, [])
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
                "source": SOURCE_OPENMETEO_SELF_PROXY,
            }
        )

    if not rows:
        return _empty_frame()
    frame = pd.DataFrame(rows, columns=list(STANDARD_COLUMNS))
    validate_standard_frame(frame, "_proxy_truth_to_long")
    return frame


def _forecasts_to_long(
    hourly: dict[str, Any],
    *,
    station: str,
    variable: str,
) -> pd.DataFrame:
    times = hourly.get("time", [])
    if not times:
        return _empty_frame()

    valid_time = pd.to_datetime(times, utc=True)
    rows: list[dict[str, Any]] = []

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
    frame = pd.DataFrame(rows, columns=list(STANDARD_COLUMNS))
    validate_standard_frame(frame, "_forecasts_to_long")
    return frame


def fetch_seoul_openmeteo_proxy_truth(
    *,
    past_days: int = PAST_DAYS,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Open-Meteo previous_day0 자기일관성 프록시 정답."""
    hourly = _fetch_hourly_payload(past_days=past_days, session=session)
    df = _proxy_truth_to_long(hourly, station=STATION_SEOUL, variable=VARIABLE_TEMPERATURE)
    if df.empty:
        raise OpenMeteoFetchError("유효한 프록시 정답 행이 없습니다.")
    return df


def fetch_seoul_ecmwf_forecasts(
    *,
    past_days: int = PAST_DAYS,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Open-Meteo ECMWF 예보 (24h/48h 리드타임)."""
    hourly = _fetch_hourly_payload(past_days=past_days, session=session)
    df = _forecasts_to_long(hourly, station=STATION_SEOUL, variable=VARIABLE_TEMPERATURE)
    if df.empty:
        raise OpenMeteoFetchError("유효한 예보 행이 없습니다.")
    return df


def fetch_seoul_temperature_slice(
    *,
    past_days: int = PAST_DAYS,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """프록시 정답 + ECMWF 예보를 한 프레임으로 (하위 호환)."""
    hourly = _fetch_hourly_payload(past_days=past_days, session=session)
    truth = _proxy_truth_to_long(hourly, station=STATION_SEOUL, variable=VARIABLE_TEMPERATURE)
    forecasts = _forecasts_to_long(hourly, station=STATION_SEOUL, variable=VARIABLE_TEMPERATURE)
    if truth.empty and forecasts.empty:
        raise OpenMeteoFetchError("유효한 기온 행이 없습니다.")
    return pd.concat([truth, forecasts], ignore_index=True)


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
                "source": SOURCE_OPENMETEO_SELF_PROXY,
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

    frame = pd.DataFrame(rows, columns=list(STANDARD_COLUMNS))
    validate_standard_frame(frame, "make_synthetic_seoul_temperature_slice")
    return frame
