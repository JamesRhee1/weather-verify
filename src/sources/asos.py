"""기상청 ASOS 시간자료 API → 표준 long-format (실측 정답)."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from src.schema import (
    SEOUL_ASOS_STN_ID,
    SOURCE_GROUND_TRUTH_ASOS,
    STANDARD_COLUMNS,
    STATION_SEOUL,
    VARIABLE_PCP,
    VARIABLE_TEMPERATURE,
    validate_standard_frame,
)
from src.sources.kma_auth import ASOS_ENDPOINTS, KST, fetch_data_go_kr, load_keys, parse_api_payload
from src.sources.store import (
    DATA_DIR,
    asos_raw_json_path,
    save_raw_json,
    upsert_parquet,
)

PAST_DAYS = 14
# ASOS API: numOfRows=1000 이면 resultCode=99 (상한 999)
_PAGE_SIZE = 999

_ASOS_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("ta", VARIABLE_TEMPERATURE),
    ("rn", VARIABLE_PCP),
)


class AsosFetchError(RuntimeError):
    """ASOS API 호출·파싱 실패."""


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(STANDARD_COLUMNS))


def asos_hourly_params(
    start_kst: datetime,
    end_kst: datetime,
    *,
    stn_id: int = SEOUL_ASOS_STN_ID,
    page_no: int = 1,
    num_of_rows: int = _PAGE_SIZE,
) -> dict[str, str | int]:
    return {
        "pageNo": page_no,
        "numOfRows": num_of_rows,
        "dataType": "JSON",
        "dataCd": "ASOS",
        "dateCd": "HR",
        "startDt": start_kst.strftime("%Y%m%d"),
        "startHh": start_kst.strftime("%H"),
        "endDt": end_kst.strftime("%Y%m%d"),
        "endHh": end_kst.strftime("%H"),
        "stnIds": str(stn_id),
    }


def parse_observation_time(tm: str) -> datetime:
    """ASOS tm (KST) → UTC."""
    obs_kst = pd.to_datetime(tm).tz_localize(KST)
    return obs_kst.to_pydatetime().astimezone(timezone.utc)


def parse_optional_float(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_qc_flag(raw: Any) -> int | None:
    """ASOS 품질검사 플래그 (0=정상, 1=오류, 9=결측)."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def parse_rn_observation(item: dict[str, Any]) -> float | None:
    """시간당 강수량(mm) — None이면 해당 시각 강수 행을 생략한다.

      공공데이터 ``getWthrDataList`` 응답은 ``rn``(강수량)과 ``rnQcflag``(품질검사)를
    함께 제공한다. 기상자료개방포털 ASOS QC 정의: 0=정상, 1=오류, 9=결측.

      - ``rnQcflag == 0``: 관측 정상. ``rn``이 비어 있으면 무강수로 0.0, 숫자면 그대로.
      - ``rnQcflag`` in (1, 9) 또는 기타: 오류·결측 → 행 생략 (0.0 대체 금지).
      - ``rnQcflag`` 없음: ``rn``이 파싱되면 사용; 빈 ``rn``은 무강수 vs 결측 구분 불가 → 생략.
        (과거에 0.0으로 채우면 실제 강수 시각이 빠져 **강수 빈도가 과소추정**된다.)
    """
    qc = parse_qc_flag(item.get("rnQcflag"))
    rn_raw = item.get("rn")

    if qc is not None:
        if qc == 0:
            value = parse_optional_float(rn_raw)
            return 0.0 if value is None else value
        return None

    return parse_optional_float(rn_raw)


def parse_asos_items_to_long(
    items: list[dict[str, Any]],
    *,
    station: str = STATION_SEOUL,
) -> pd.DataFrame:
    """ASOS item 목록 → 표준 long-format (기온·강수량, lead_time_h=0)."""
    rows: list[dict[str, Any]] = []
    for item in items:
        tm = item.get("tm")
        if not tm:
            continue
        valid_time = parse_observation_time(str(tm))

        for api_field, variable in _ASOS_FIELD_MAP:
            if api_field == "rn":
                value = parse_rn_observation(item)
            else:
                value = parse_optional_float(item.get(api_field))
            if value is None:
                continue
            rows.append(
                {
                    "station": station,
                    "valid_time": valid_time,
                    "lead_time_h": 0,
                    "variable": variable,
                    "value": value,
                    "source": SOURCE_GROUND_TRUTH_ASOS,
                }
            )

    if not rows:
        return _empty_frame()

    frame = pd.DataFrame(rows, columns=list(STANDARD_COLUMNS))
    validate_standard_frame(frame, "parse_asos_items_to_long")
    return frame


def parse_asos_payload(
    payload: dict[str, Any],
    *,
    station: str = STATION_SEOUL,
) -> pd.DataFrame:
    code, msg, items = parse_api_payload(payload)
    if code != "00":
        raise ValueError(f"resultCode={code} msg={msg}")
    return parse_asos_items_to_long(items, station=station)


def _default_time_window_kst(
    *,
    past_days: int,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """ASOS 조회 구간 (KST). API는 전일(D-1)까지 제공."""
    now_kst = (now or datetime.now(KST)).astimezone(KST)
    end_kst = (now_kst - timedelta(days=1)).replace(minute=0, second=0, microsecond=0, hour=23)
    start_kst = (end_kst - timedelta(days=past_days - 1)).replace(hour=0)
    return start_kst, end_kst


def yesterday_window_kst(now: datetime | None = None) -> tuple[datetime, datetime]:
    """전일(D-1) 00~23시 (일 1회 적재용)."""
    return _default_time_window_kst(past_days=1, now=now)


def fetch_asos_hourly_all_pages(
    start_kst: datetime,
    end_kst: datetime,
    session: requests.Session,
    *,
    stn_id: int = SEOUL_ASOS_STN_ID,
    decoding_key: str | None = None,
    encoding_key: str | None = None,
) -> list[dict[str, Any]]:
    """페이지네이션 처리해 전체 item 반환."""
    page = 1
    all_items: list[dict[str, Any]] = []
    total_count: int | None = None

    while True:
        params = asos_hourly_params(start_kst, end_kst, stn_id=stn_id, page_no=page)
        payload = fetch_data_go_kr(
            ASOS_ENDPOINTS,
            params,
            session,
            decoding_key=decoding_key,
            encoding_key=encoding_key,
        )
        code, msg, items = parse_api_payload(payload)
        if code != "00":
            raise AsosFetchError(f"resultCode={code} msg={msg}")

        body = payload.get("response", {}).get("body", {})
        if total_count is None:
            total_count = int(body.get("totalCount", len(items)))

        all_items.extend(items)
        if len(all_items) >= total_count or not items:
            break
        page += 1

    return all_items


def fetch_seoul_asos_slice(
    *,
    past_days: int = PAST_DAYS,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """서울 ASOS 시간자료 — 기온·강수량 실측."""
    decoding, encoding = load_keys()
    if not decoding and not encoding:
        raise AsosFetchError("공공데이터 API 키가 설정되지 않았습니다.")

    start_kst, end_kst = _default_time_window_kst(past_days=past_days, now=now)
    sess = session or requests.Session()
    items = fetch_asos_hourly_all_pages(
        start_kst,
        end_kst,
        sess,
        decoding_key=decoding,
        encoding_key=encoding,
    )
    frame = parse_asos_items_to_long(items)
    if frame.empty:
        raise AsosFetchError("유효한 ASOS 관측 행이 없습니다.")
    return frame


def stage_asos_for_storage(frame: pd.DataFrame) -> pd.DataFrame:
    """ASOS 관측: issue_time = valid_time (lead_time_h=0)."""
    staged = frame.copy()
    staged["issue_time"] = staged["valid_time"]
    return staged


def partition_frames_by_obs_date(frame: pd.DataFrame) -> dict[date, pd.DataFrame]:
    """관측일(KST)별로 프레임 분할."""
    if frame.empty:
        return {}
    obs_dates = frame["valid_time"].dt.tz_convert(KST).dt.date
    parts: dict[date, pd.DataFrame] = {}
    for obs_date in sorted(obs_dates.unique()):
        mask = obs_dates == obs_date
        parts[obs_date] = frame.loc[mask].copy()
    return parts


def collect_asos_daily(
    *,
    station: str = STATION_SEOUL,
    data_dir: Path = DATA_DIR,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """전일(D-1) ASOS 시간자료 적재 — source=ground_truth_asos 파티션."""
    decoding, encoding = load_keys()
    if not decoding and not encoding:
        raise AsosFetchError("공공데이터 API 키가 설정되지 않았습니다.")

    start_kst, end_kst = yesterday_window_kst(now=now)
    sess = session or requests.Session()
    items = fetch_asos_hourly_all_pages(
        start_kst,
        end_kst,
        sess,
        decoding_key=decoding,
        encoding_key=encoding,
    )
    frame = parse_asos_items_to_long(items, station=station)
    if frame.empty:
        raise AsosFetchError("유효한 ASOS 관측 행이 없습니다.")

    obs_date = start_kst.date()
    raw_path = save_raw_json(
        {"items": items, "start_kst": start_kst.isoformat(), "end_kst": end_kst.isoformat()},
        asos_raw_json_path(data_dir, obs_date=obs_date, station=station),
    )

    staged = stage_asos_for_storage(frame)
    parquet_paths: list[Path] = []
    for part_date, part in partition_frames_by_obs_date(staged).items():
        path = upsert_parquet(
            part,
            data_dir=data_dir,
            issue_date=part_date,
            source=SOURCE_GROUND_TRUTH_ASOS,
        )
        parquet_paths.append(path)

    print("=== ASOS 실측 적재 완료 ===")
    print(f"관측일(KST) : {obs_date}")
    print(f"행 수       : {len(frame)}")
    print(f"raw JSON    : {raw_path}")
    for path in parquet_paths:
        print(f"parquet     : {path}")
    return parquet_paths


def run_collect() -> int:
    try:
        collect_asos_daily()
    except AsosFetchError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ASOS 시간자료 적재")
    parser.add_argument("--collect", action="store_true", help="전일(D-1) 실측 수집·저장")
    args = parser.parse_args(argv)

    if args.collect:
        return run_collect()

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
