"""Open-Meteo Previous Runs API → 표준 long-format."""

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
    save_raw_json,
    upsert_parquet,
)

PREVIOUS_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
SEOUL_LAT = 37.5665
SEOUL_LON = 126.9780
PAST_DAYS = 14
COLLECT_PAST_DAYS = 1

# (api_model_name, source 라벨)
OPENMETEO_MODELS: tuple[tuple[str, str], ...] = (
    ("ecmwf_ifs", SOURCE_OPENMETEO_ECMWF),
    ("gfs_global", SOURCE_OPENMETEO_GFS),
)

# (hourly base_name, 표준 variable, (days_ahead, lead_time_h 근사)…)
# lead_time_h·days_ahead 근사 한계는 README "Open-Meteo 리드타임 근사" 참고.
FORECAST_VARIABLE_SPECS: tuple[tuple[str, str, tuple[tuple[int, int], ...]], ...] = (
    ("temperature_2m", VARIABLE_TEMPERATURE, ((1, 24), (2, 48))),
    ("precipitation_probability", VARIABLE_POP, ((1, 24), (2, 48))),
)

_PROXY_TRUTH_KEY = "temperature_2m"
_MODEL_CYCLE_HOURS = 6

# 하위 호환: ECMWF 기온만 (단일 모델 API 키 — 모델 접미사 없음)
_FORECAST_MAP: tuple[tuple[str, int, str], ...] = (
    ("temperature_2m_previous_day1", 24, SOURCE_OPENMETEO_ECMWF),  # ≈ days_ahead=1
    ("temperature_2m_previous_day2", 48, SOURCE_OPENMETEO_ECMWF),  # ≈ days_ahead=2
)


class OpenMeteoFetchError(RuntimeError):
    """Previous Runs API 호출 실패."""


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(STANDARD_COLUMNS))


def days_ahead_from_lead(lead_time_h: int) -> int:
    """lead_time_h 근사 라벨(24, 48…) → 발표일 차이 days_ahead."""
    return max(1, lead_time_h // 24)


def approximate_issue_time(valid_time: pd.Timestamp, days_ahead: int) -> datetime:
    """Previous Runs run 초기화 시각 근사 (UTC).

    Open-Meteo Previous Runs는 valid_time마다 서로 다른 초기화 run을 블렌딩한
    시계열을 반환하므로 행마다 단일 ``issue_time`` 은 근사치다. 실제로 day1 값은
    24~47h 리드 구간의 0/6/12/18Z run이 시간대별로 섞인다.

    근사: ``valid_time`` 에서 ``days_ahead`` 일을 뺀 뒤, 시각을 6h 주기(0/6/12/18Z)로
    내림한 UTC 시각. KMA 발표시각과 1:1 대응하지 않으며, 비교 시 ``days_ahead``
    버킷팅을 사용할 것.
    """
    vt = pd.Timestamp(valid_time).tz_convert(timezone.utc)
    cycle_hour = (vt.hour // _MODEL_CYCLE_HOURS) * _MODEL_CYCLE_HOURS
    issue_day = (vt - timedelta(days=days_ahead)).normalize()
    issue = issue_day + timedelta(hours=cycle_hour)
    return issue.to_pydatetime()


def _hourly_field_names(
    models: tuple[str, ...],
    *,
    include_proxy: bool,
    variables: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] | None = None,
) -> list[str]:
    """Previous Runs API ``hourly`` 파라미터 목록."""
    names: list[str] = []
    if include_proxy:
        names.append(f"{_PROXY_TRUTH_KEY}_previous_day0")

    var_specs = (
        tuple((base, offsets) for base, _, offsets in FORECAST_VARIABLE_SPECS)
        if variables is None
        else variables
    )
    for base_name, day_offsets in var_specs:
        for days_ahead, _ in day_offsets:
            # 요청 파라미터는 모델 접미사 없음; 응답 키만 _{model} 접미사 (다중 모델 시).
            names.append(f"{base_name}_previous_day{days_ahead}")
    return names


def _forecast_entries(
    models: tuple[str, ...],
    *,
    variables: tuple[tuple[str, str, tuple[tuple[int, int], ...]], ...] | None = None,
) -> tuple[tuple[str, int, str, str], ...]:
    """(api_key, lead_time_h, source, variable) 목록."""
    entries: list[tuple[str, int, str, str]] = []
    multi_model = len(models) > 1
    var_specs = variables or FORECAST_VARIABLE_SPECS

    for model_api, source in OPENMETEO_MODELS:
        if model_api not in models:
            continue
        for base_name, variable, day_offsets in var_specs:
            for days_ahead, lead_h in day_offsets:
                stem = f"{base_name}_previous_day{days_ahead}"
                api_key = f"{stem}_{model_api}" if multi_model else stem
                entries.append((api_key, lead_h, source, variable))
    return tuple(entries)


def _fetch_hourly_payload(
    *,
    past_days: int = PAST_DAYS,
    models: tuple[str, ...] = (OPENMETEO_MODELS[0][0],),
    include_proxy: bool = False,
    variables: tuple[tuple[str, str, tuple[tuple[int, int], ...]], ...] | None = None,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    params = {
        "latitude": SEOUL_LAT,
        "longitude": SEOUL_LON,
        "hourly": ",".join(
            _hourly_field_names(models, include_proxy=include_proxy, variables=variables)
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


def _forecasts_to_long(
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
    validate_standard_frame(frame, "_forecasts_to_long")
    return frame


def attach_forecast_issue_times(frame: pd.DataFrame) -> pd.DataFrame:
    """예보 행에 근사 ``issue_time`` 부착 (저장·upsert 용, 표준 6컬럼 외)."""
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    issue_times = [
        approximate_issue_time(vt, days_ahead_from_lead(int(lt)))
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
    """Open-Meteo previous_day0 자기일관성 프록시 정답."""
    hourly = _fetch_hourly_payload(
        past_days=past_days,
        models=(OPENMETEO_MODELS[0][0],),
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
    """Open-Meteo ECMWF 예보 (lead_time_h=24/48은 days_ahead 1/2 근사 라벨)."""
    hourly = _fetch_hourly_payload(past_days=past_days, session=session)
    df = _forecasts_to_long(
        hourly,
        station=STATION_SEOUL,
        entries=_FORECAST_MAP,
        default_variable=VARIABLE_TEMPERATURE,
    )
    if df.empty:
        raise OpenMeteoFetchError("유효한 예보 행이 없습니다.")
    return df


def fetch_seoul_pop_forecasts(
    *,
    past_days: int = PAST_DAYS,
    models: tuple[str, ...] | None = None,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Open-Meteo 강수확률 예보 (0~100 %, /100 변환은 지표 호출부 책임)."""
    model_apis = models or tuple(api for api, _ in OPENMETEO_MODELS)
    pop_specs: tuple[tuple[str, tuple[tuple[int, int], ...]], ...] = (
        ("precipitation_probability", ((1, 24), (2, 48))),
    )
    hourly = _fetch_hourly_payload(
        past_days=past_days,
        models=model_apis,
        variables=pop_specs,
        session=session,
    )
    entries = _forecast_entries(
        model_apis,
        variables=(("precipitation_probability", VARIABLE_POP, ((1, 24), (2, 48))),),
    )
    df = _forecasts_to_long(hourly, station=STATION_SEOUL, entries=entries)
    if df.empty:
        raise OpenMeteoFetchError("유효한 강수확률 예보 행이 없습니다.")
    return df


def fetch_seoul_multi_model_forecasts(
    *,
    past_days: int = PAST_DAYS,
    models: tuple[str, ...] | None = None,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """ECMWF·GFS 등 다중 모델 기온·강수확률 예보."""
    model_apis = models or tuple(api for api, _ in OPENMETEO_MODELS)
    hourly = _fetch_hourly_payload(past_days=past_days, models=model_apis, session=session)
    entries = _forecast_entries(model_apis)
    df = _forecasts_to_long(hourly, station=STATION_SEOUL, entries=entries)
    if df.empty:
        raise OpenMeteoFetchError("유효한 예보 행이 없습니다.")
    return df


def fetch_seoul_temperature_slice(
    *,
    past_days: int = PAST_DAYS,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """프록시 정답 + ECMWF 예보를 한 프레임으로 (하위 호환)."""
    hourly = _fetch_hourly_payload(
        past_days=past_days,
        models=(OPENMETEO_MODELS[0][0],),
        include_proxy=True,
        session=session,
    )
    truth = _proxy_truth_to_long(hourly, station=STATION_SEOUL, variable=VARIABLE_TEMPERATURE)
    forecasts = _forecasts_to_long(
        hourly,
        station=STATION_SEOUL,
        entries=_FORECAST_MAP,
        default_variable=VARIABLE_TEMPERATURE,
    )
    if truth.empty and forecasts.empty:
        raise OpenMeteoFetchError("유효한 기온 행이 없습니다.")
    return pd.concat([truth, forecasts], ignore_index=True)


def collect_openmeteo_daily(
    *,
    station: str = STATION_SEOUL,
    data_dir: Path = DATA_DIR,
    past_days: int = COLLECT_PAST_DAYS,
    models: tuple[str, ...] | None = None,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """Open-Meteo Previous Runs 예보 적재 — 모델·변수별 source 파티션."""
    model_apis = models or tuple(api for api, _ in OPENMETEO_MODELS)
    sess = session or requests.Session()
    hourly = _fetch_hourly_payload(past_days=past_days, models=model_apis, session=sess)
    entries = _forecast_entries(model_apis)
    frame = _forecasts_to_long(hourly, station=station, entries=entries)
    if frame.empty:
        raise OpenMeteoFetchError("유효한 Open-Meteo 예보 행이 없습니다.")

    collected_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    raw_path = save_raw_json(
        {"hourly": hourly, "models": list(model_apis), "collected_at": collected_at.isoformat()},
        data_dir
        / "raw"
        / "openmeteo"
        / f"collect_{collected_at.strftime('%Y%m%d_%H%M')}_{station}.json",
    )

    staged = attach_forecast_issue_times(frame)
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

    print("=== Open-Meteo 예보 적재 완료 ===")
    print(f"모델        : {', '.join(model_apis)}")
    print(f"행 수       : {len(frame)}")
    print(f"변수        : {sorted(frame['variable'].unique())}")
    print(f"raw JSON    : {raw_path}")
    for path in parquet_paths:
        print(f"parquet     : {path}")
    return parquet_paths


def run_collect() -> int:
    try:
        collect_openmeteo_daily()
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
    parser = argparse.ArgumentParser(description="Open-Meteo Previous Runs 적재")
    parser.add_argument(
        "--collect",
        action="store_true",
        help="최근 예보(기온·강수확률, ECMWF+GFS) 수집·parquet 저장",
    )
    args = parser.parse_args(argv)

    if args.collect:
        return run_collect()

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
