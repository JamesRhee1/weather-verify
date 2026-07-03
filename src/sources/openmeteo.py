"""Open-Meteo Forecast / Previous Runs API → 표준 long-format."""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from src.schema import (
    OPENMETEO_FORECAST_SOURCES,
    SOURCE_OPENMETEO_ECMWF,
    SOURCE_OPENMETEO_GFS,
    SOURCE_OPENMETEO_SELF_PROXY,
    STANDARD_COLUMNS,
    STATION_SEOUL,
    VARIABLE_POP,
    VARIABLE_TEMPERATURE,
    validate_standard_frame,
)
from src.sources.store import (
    DATA_DIR,
    attach_issue_time,
    save_raw_json,
    upsert_parquet,
)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
SEOUL_LAT = 37.5665
SEOUL_LON = 126.9780
PAST_DAYS = 14
LEGACY_COLLECT_PAST_DAYS = 1
FORWARD_FORECAST_DAYS = 3
MIN_FORWARD_LEAD_TIME_H = 1

# 전향 수집 — Forecast API (POP 포함, 자체 아카이브)
FORWARD_MODELS: tuple[tuple[str, str], ...] = (
    ("ecmwf_ifs025", SOURCE_OPENMETEO_ECMWF),
    ("gfs_seamless", SOURCE_OPENMETEO_GFS),
)
FORWARD_HOURLY_FIELDS: tuple[tuple[str, str], ...] = (
    ("temperature_2m", VARIABLE_TEMPERATURE),
    ("precipitation_probability", VARIABLE_POP),
)

# legacy Previous Runs — 기온 백테스트·run_slice 용 (POP 미제공)
LEGACY_PREVIOUS_RUNS_MODELS: tuple[tuple[str, str], ...] = (
    ("ecmwf_ifs", SOURCE_OPENMETEO_ECMWF),
    ("gfs_global", SOURCE_OPENMETEO_GFS),
)
LEGACY_FORECAST_VARIABLE_SPECS: tuple[tuple[str, str, tuple[tuple[int, int], ...]], ...] = (
    ("temperature_2m", VARIABLE_TEMPERATURE, ((1, 24), (2, 48))),
    ("precipitation_probability", VARIABLE_POP, ((1, 24), (2, 48))),
)

_PROXY_TRUTH_KEY = "temperature_2m"
_MODEL_CYCLE_HOURS = 6

# 하위 호환: ECMWF 기온만 (단일 모델 Previous Runs API 키)
_LEGACY_FORECAST_MAP: tuple[tuple[str, int, str], ...] = (
    ("temperature_2m_previous_day1", 24, SOURCE_OPENMETEO_ECMWF),
    ("temperature_2m_previous_day2", 48, SOURCE_OPENMETEO_ECMWF),
)


class OpenMeteoFetchError(RuntimeError):
    """Open-Meteo API 호출·파싱 실패."""


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(STANDARD_COLUMNS))


def truncate_issue_time(now: datetime | None = None) -> datetime:
    """전향 수집 ``issue_time`` — UTC 시간 단위 절사.

      실제 모델 초기화 시각(00/06/12/18Z)과 다를 수 있는 **수집 시각 근사**이다.
    KMA ``baseDate/baseTime`` 과 1:1 대응하지 않으며, 비교 기산일은 이 시각의
      UTC 날짜(수집 시작일)이다.
    """
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return ts.replace(minute=0, second=0, microsecond=0)


def compute_forward_lead_time_h(issue_time: datetime, valid_time: pd.Timestamp) -> int:
    """``valid_time - issue_time`` 을 정수 시간으로."""
    issue = issue_time.astimezone(timezone.utc)
    valid = pd.Timestamp(valid_time).tz_convert(timezone.utc)
    return int((valid.to_pydatetime() - issue).total_seconds() // 3600)


def days_ahead_from_lead(lead_time_h: int) -> int:
    """legacy Previous Runs: lead_time_h 근사 라벨 → days_ahead."""
    return max(1, lead_time_h // 24)


def approximate_legacy_issue_time(valid_time: pd.Timestamp, days_ahead: int) -> datetime:
    """legacy Previous Runs run 초기화 시각 근사 (UTC).

    블렌딩된 previous_day 시계열용. 전향 아카이브에는 사용하지 않는다.
    """
    vt = pd.Timestamp(valid_time).tz_convert(timezone.utc)
    cycle_hour = (vt.hour // _MODEL_CYCLE_HOURS) * _MODEL_CYCLE_HOURS
    issue_day = (vt - timedelta(days=days_ahead)).normalize()
    issue = issue_day + timedelta(hours=cycle_hour)
    return issue.to_pydatetime()


def _fetch_forward_hourly_payload(
    *,
    forecast_days: int = FORWARD_FORECAST_DAYS,
    models: tuple[str, ...] | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Forecast API — 지금 발표된 미래 예보 (POP 포함)."""
    model_apis = models or tuple(api for api, _ in FORWARD_MODELS)
    params = {
        "latitude": SEOUL_LAT,
        "longitude": SEOUL_LON,
        "hourly": ",".join(field for field, _ in FORWARD_HOURLY_FIELDS),
        "models": ",".join(model_apis),
        "forecast_days": forecast_days,
        "timezone": "UTC",
    }
    sess = session or requests.Session()
    resp = sess.get(FORECAST_URL, params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error"):
        raise OpenMeteoFetchError(str(payload.get("reason", payload)))
    hourly = payload.get("hourly")
    if not hourly:
        raise OpenMeteoFetchError("hourly 데이터가 없습니다.")
    return hourly


def parse_forward_hourly_to_long(
    hourly: dict[str, Any],
    *,
    issue_time: datetime,
    station: str = STATION_SEOUL,
    models: tuple[tuple[str, str], ...] | None = None,
    min_lead_time_h: int = MIN_FORWARD_LEAD_TIME_H,
) -> pd.DataFrame:
    """Forecast API hourly → 표준 long-format.

    ``issue_time`` 은 수집 시각(UTC, 시간 절사)이며 모델 init 시각의 근사이다.
    ``lead_time_h`` = ``valid_time - issue_time`` (정수 시간). ``lead_time_h < min`` 행 생략.
    POP 값은 0~100 % 그대로 (/100 변환은 지표 호출부 책임).
    """
    times = hourly.get("time", [])
    if not times:
        return _empty_frame()

    valid_times = pd.to_datetime(times, utc=True)
    rows: list[dict[str, Any]] = []
    model_specs = models or FORWARD_MODELS

    for model_api, source in model_specs:
        for field_name, variable in FORWARD_HOURLY_FIELDS:
            api_key = f"{field_name}_{model_api}"
            values = hourly.get(api_key, [])
            for valid_time, val in zip(valid_times, values, strict=False):
                if val is None or (isinstance(val, float) and math.isnan(val)):
                    continue
                lead_h = compute_forward_lead_time_h(issue_time, valid_time)
                if lead_h < min_lead_time_h:
                    continue
                rows.append(
                    {
                        "station": station,
                        "valid_time": valid_time,
                        "lead_time_h": lead_h,
                        "variable": variable,
                        "value": float(val),
                        "source": source,
                    }
                )

    if not rows:
        return _empty_frame()
    frame = pd.DataFrame(rows, columns=list(STANDARD_COLUMNS))
    validate_standard_frame(frame, "parse_forward_hourly_to_long")
    return frame


def fetch_forward_forecasts(
    *,
    forecast_days: int = FORWARD_FORECAST_DAYS,
    models: tuple[str, ...] | None = None,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Forecast API 전향 예보 — 기온·POP, ECMWF·GFS."""
    issue_time = truncate_issue_time(now)
    hourly = _fetch_forward_hourly_payload(
        forecast_days=forecast_days,
        models=models,
        session=session,
    )
    frame = parse_forward_hourly_to_long(hourly, issue_time=issue_time)
    if frame.empty:
        raise OpenMeteoFetchError("유효한 전향 예보 행이 없습니다.")
    return frame


def collect_openmeteo_forward(
    *,
    station: str = STATION_SEOUL,
    data_dir: Path = DATA_DIR,
    forecast_days: int = FORWARD_FORECAST_DAYS,
    models: tuple[str, ...] | None = None,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """전향 Forecast API 예보 적재 — KMA ``--collect`` 와 동일 방법론.

    ``issue_time`` = 수집 시각(UTC, 시간 절사). 파티션 ``issue_date`` 는 그 UTC 날짜.
    비교·검증 시 **기산일 = 수집 시작일(issue_date)** 로 해석한다.
    """
    issue_time = truncate_issue_time(now)
    issue_date = issue_time.date()
    model_apis = models or tuple(api for api, _ in FORWARD_MODELS)
    sess = session or requests.Session()
    hourly = _fetch_forward_hourly_payload(
        forecast_days=forecast_days,
        models=model_apis,
        session=sess,
    )
    frame = parse_forward_hourly_to_long(hourly, issue_time=issue_time, station=station)
    if frame.empty:
        raise OpenMeteoFetchError("유효한 전향 예보 행이 없습니다.")

    raw_path = save_raw_json(
        {
            "hourly": hourly,
            "models": list(model_apis),
            "issue_time": issue_time.isoformat(),
            "mode": "forward",
        },
        data_dir
        / "raw"
        / "openmeteo"
        / f"forward_{issue_time.strftime('%Y%m%d_%H')}_{station}.json",
    )

    staged = attach_issue_time(frame, issue_time)
    parquet_paths: list[Path] = []
    for source in sorted(staged["source"].unique()):
        if source not in OPENMETEO_FORECAST_SOURCES:
            continue
        part = staged[staged["source"] == source]
        path = upsert_parquet(
            part,
            data_dir=data_dir,
            issue_date=issue_date,
            source=source,
        )
        parquet_paths.append(path)

    print("=== Open-Meteo 전향 예보 적재 완료 ===")
    print(f"issue_time  : {issue_time.isoformat()} (수집 시각 근사, UTC)")
    print(f"issue_date  : {issue_date} (비교 기산일)")
    print(f"모델        : {', '.join(model_apis)}")
    print(f"행 수       : {len(frame)}")
    print(f"변수        : {sorted(frame['variable'].unique())}")
    print(f"raw JSON    : {raw_path}")
    for path in parquet_paths:
        print(f"parquet     : {path}")
    return parquet_paths


# --- legacy Previous Runs (기온 백테스트·run_slice; POP 미제공) ---


def _legacy_hourly_field_names(
    models: tuple[str, ...],
    *,
    include_proxy: bool,
    variables: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] | None = None,
) -> list[str]:
    names: list[str] = []
    if include_proxy:
        names.append(f"{_PROXY_TRUTH_KEY}_previous_day0")

    var_specs = (
        tuple((base, offsets) for base, _, offsets in LEGACY_FORECAST_VARIABLE_SPECS)
        if variables is None
        else variables
    )
    for base_name, day_offsets in var_specs:
        for days_ahead, _ in day_offsets:
            names.append(f"{base_name}_previous_day{days_ahead}")
    return names


def _legacy_forecast_entries(
    models: tuple[str, ...],
    *,
    variables: tuple[tuple[str, str, tuple[tuple[int, int], ...]], ...] | None = None,
) -> tuple[tuple[str, int, str, str], ...]:
    entries: list[tuple[str, int, str, str]] = []
    multi_model = len(models) > 1
    var_specs = variables or LEGACY_FORECAST_VARIABLE_SPECS

    for model_api, source in LEGACY_PREVIOUS_RUNS_MODELS:
        if model_api not in models:
            continue
        for base_name, variable, day_offsets in var_specs:
            for days_ahead, lead_h in day_offsets:
                stem = f"{base_name}_previous_day{days_ahead}"
                api_key = f"{stem}_{model_api}" if multi_model else stem
                entries.append((api_key, lead_h, source, variable))
    return tuple(entries)


def _fetch_legacy_previous_runs_payload(
    *,
    past_days: int = PAST_DAYS,
    models: tuple[str, ...] = (LEGACY_PREVIOUS_RUNS_MODELS[0][0],),
    include_proxy: bool = False,
    variables: tuple[tuple[str, str, tuple[tuple[int, int], ...]], ...] | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """legacy Previous Runs API.

    ``precipitation_probability_previous_dayN`` 은 API상 전부 null(파생 변수 미제공).
    POP 아카이브·검증에는 ``fetch_forward_forecasts`` / ``--collect`` 를 사용할 것.
    """
    params = {
        "latitude": SEOUL_LAT,
        "longitude": SEOUL_LON,
        "hourly": ",".join(
            _legacy_hourly_field_names(models, include_proxy=include_proxy, variables=variables)
        ),
        "models": ",".join(models),
        "past_days": past_days,
        "timezone": "UTC",
    }
    sess = session or requests.Session()
    resp = sess.get(PREVIOUS_RUNS_URL, params=params, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("error"):
        raise OpenMeteoFetchError(str(payload.get("reason", payload)))
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
    truth_vals = hourly.get(_PROXY_TRUTH_KEY, hourly.get(f"{_PROXY_TRUTH_KEY}_previous_day0", []))
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


def _legacy_forecasts_to_long(
    hourly: dict[str, Any],
    *,
    station: str,
    entries: tuple[tuple[str, int, str, str], ...] | tuple[tuple[str, int, str], ...],
    default_variable: str | None = None,
) -> pd.DataFrame:
    times = hourly.get("time", [])
    if not times:
        return _empty_frame()

    valid_time = pd.to_datetime(times, utc=True)
    rows: list[dict[str, Any]] = []

    for entry in entries:
        if len(entry) == 4:
            api_key, lead_h, source, variable = entry
        else:
            api_key, lead_h, source = entry
            variable = default_variable or VARIABLE_TEMPERATURE

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
    validate_standard_frame(frame, "_legacy_forecasts_to_long")
    return frame


def attach_legacy_forecast_issue_times(frame: pd.DataFrame) -> pd.DataFrame:
    """legacy Previous Runs 행에 근사 ``issue_time`` 부착."""
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    issue_times = [
        approximate_legacy_issue_time(vt, days_ahead_from_lead(int(lt)))
        for vt, lt in zip(out["valid_time"], out["lead_time_h"], strict=False)
    ]
    out["issue_time"] = issue_times
    return out


def partition_frames_by_source_issue_date(
    frame: pd.DataFrame,
) -> dict[tuple[str, date], pd.DataFrame]:
    """(source, issue_date UTC) 별 프레임 분할."""
    if frame.empty or "issue_time" not in frame.columns:
        return {}
    issue_dates = pd.to_datetime(frame["issue_time"], utc=True).dt.date
    parts: dict[tuple[str, date], pd.DataFrame] = {}
    for source in sorted(frame["source"].unique()):
        for issue_date in sorted(issue_dates[frame["source"] == source].unique()):
            mask = (frame["source"] == source) & (issue_dates == issue_date)
            parts[(source, issue_date)] = frame.loc[mask].copy()
    return parts


def fetch_seoul_openmeteo_proxy_truth(
    *,
    past_days: int = PAST_DAYS,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Open-Meteo previous_day0 자기일관성 프록시 정답 (legacy Previous Runs)."""
    hourly = _fetch_legacy_previous_runs_payload(
        past_days=past_days,
        models=(LEGACY_PREVIOUS_RUNS_MODELS[0][0],),
        include_proxy=True,
        variables=(),
        session=session,
    )
    df = _proxy_truth_to_long(hourly, station=STATION_SEOUL, variable=VARIABLE_TEMPERATURE)
    if df.empty:
        raise OpenMeteoFetchError("유효한 프록시 정답 행이 없습니다.")
    return df


def fetch_seoul_ecmwf_forecasts(
    *,
    past_days: int = PAST_DAYS,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """legacy ECMWF 기온 예보 (lead_time_h=24/48은 days_ahead 근사 라벨)."""
    hourly = _fetch_legacy_previous_runs_payload(past_days=past_days, session=session)
    df = _legacy_forecasts_to_long(
        hourly,
        station=STATION_SEOUL,
        entries=_LEGACY_FORECAST_MAP,
        default_variable=VARIABLE_TEMPERATURE,
    )
    if df.empty:
        raise OpenMeteoFetchError("유효한 예보 행이 없습니다.")
    return df


def fetch_seoul_legacy_pop_forecasts(
    *,
    past_days: int = PAST_DAYS,
    models: tuple[str, ...] | None = None,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """legacy Previous Runs POP — API상 전부 null일 수 있음. 전향 수집 권장."""
    model_apis = models or tuple(api for api, _ in LEGACY_PREVIOUS_RUNS_MODELS)
    pop_specs: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
        ("precipitation_probability", ((1, 24), (2, 48))),
    )
    hourly = _fetch_legacy_previous_runs_payload(
        past_days=past_days,
        models=model_apis,
        variables=pop_specs,
        session=session,
    )
    entries = _legacy_forecast_entries(
        model_apis,
        variables=(("precipitation_probability", VARIABLE_POP, ((1, 24), (2, 48))),),
    )
    df = _legacy_forecasts_to_long(hourly, station=STATION_SEOUL, entries=entries)
    if df.empty:
        raise OpenMeteoFetchError("유효한 강수확률 예보 행이 없습니다.")
    return df


def fetch_seoul_legacy_multi_model_forecasts(
    *,
    past_days: int = PAST_DAYS,
    models: tuple[str, ...] | None = None,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """legacy ECMWF·GFS 기온·POP (POP는 대개 비어 있음)."""
    model_apis = models or tuple(api for api, _ in LEGACY_PREVIOUS_RUNS_MODELS)
    hourly = _fetch_legacy_previous_runs_payload(
        past_days=past_days, models=model_apis, session=session
    )
    entries = _legacy_forecast_entries(model_apis)
    df = _legacy_forecasts_to_long(hourly, station=STATION_SEOUL, entries=entries)
    if df.empty:
        raise OpenMeteoFetchError("유효한 예보 행이 없습니다.")
    return df


def fetch_seoul_temperature_slice(
    *,
    past_days: int = PAST_DAYS,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """프록시 정답 + ECMWF 예보 (legacy, 하위 호환)."""
    hourly = _fetch_legacy_previous_runs_payload(
        past_days=past_days,
        models=(LEGACY_PREVIOUS_RUNS_MODELS[0][0],),
        include_proxy=True,
        session=session,
    )
    truth = _proxy_truth_to_long(hourly, station=STATION_SEOUL, variable=VARIABLE_TEMPERATURE)
    forecasts = _legacy_forecasts_to_long(
        hourly,
        station=STATION_SEOUL,
        entries=_LEGACY_FORECAST_MAP,
        default_variable=VARIABLE_TEMPERATURE,
    )
    if truth.empty and forecasts.empty:
        raise OpenMeteoFetchError("유효한 기온 행이 없습니다.")
    return pd.concat([truth, forecasts], ignore_index=True)


def collect_openmeteo_legacy_previous_runs(
    *,
    station: str = STATION_SEOUL,
    data_dir: Path = DATA_DIR,
    past_days: int = LEGACY_COLLECT_PAST_DAYS,
    models: tuple[str, ...] | None = None,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """legacy Previous Runs 적재 — 기온 백테스트용. POP 미제공."""
    model_apis = models or tuple(api for api, _ in LEGACY_PREVIOUS_RUNS_MODELS)
    sess = session or requests.Session()
    hourly = _fetch_legacy_previous_runs_payload(
        past_days=past_days, models=model_apis, session=sess
    )
    entries = _legacy_forecast_entries(model_apis)
    frame = _legacy_forecasts_to_long(hourly, station=station, entries=entries)
    if frame.empty:
        raise OpenMeteoFetchError("유효한 legacy 예보 행이 없습니다.")

    collected_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw_path = save_raw_json(
        {
            "hourly": hourly,
            "models": list(model_apis),
            "collected_at": collected_at.isoformat(),
            "mode": "legacy_previous_runs",
        },
        data_dir
        / "raw"
        / "openmeteo"
        / f"legacy_{collected_at.strftime('%Y%m%d_%H%M')}_{station}.json",
    )

    staged = attach_legacy_forecast_issue_times(frame)
    parquet_paths: list[Path] = []
    for (source, issue_date), part in partition_frames_by_source_issue_date(staged).items():
        if source not in OPENMETEO_FORECAST_SOURCES:
            continue
        path = upsert_parquet(
            part,
            data_dir=data_dir,
            issue_date=issue_date,
            source=source,
        )
        parquet_paths.append(path)

    print("=== Open-Meteo legacy Previous Runs 적재 완료 ===")
    print(f"모델        : {', '.join(model_apis)}")
    print(f"행 수       : {len(frame)}")
    print(f"변수        : {sorted(frame['variable'].unique())}")
    print(f"raw JSON    : {raw_path}")
    for path in parquet_paths:
        print(f"parquet     : {path}")
    return parquet_paths


def run_collect() -> int:
    try:
        collect_openmeteo_forward()
    except OpenMeteoFetchError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    return 0


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open-Meteo 예보 적재")
    parser.add_argument(
        "--collect",
        action="store_true",
        help="전향 Forecast API 예보(기온·POP) 수집·parquet 저장",
    )
    parser.add_argument(
        "--collect-legacy",
        action="store_true",
        help="legacy Previous Runs 적재 (기온 백테스트용, POP 미제공)",
    )
    args = parser.parse_args(argv)

    if args.collect:
        return run_collect()
    if args.collect_legacy:
        try:
            collect_openmeteo_legacy_previous_runs()
        except OpenMeteoFetchError as exc:
            print(f"[error] {exc}", file=sys.stderr)
            return 1
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
