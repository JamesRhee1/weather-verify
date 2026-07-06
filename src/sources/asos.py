"""기상청 ASOS 시간자료 API → 표준 long-format (실측 정답)."""

from __future__ import annotations

import argparse
import sys
import time
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
from src.sources.kma_auth import (
    ASOS_ENDPOINTS,
    KST,
    REQUEST_INTERVAL_SEC,
    fetch_data_go_kr,
    load_keys,
    parse_api_payload,
)
from src.sources.store import (
    DATA_DIR,
    asos_raw_json_path,
    load_stored_issue_dates,
    save_raw_json,
    upsert_parquet,
)

BACKFILL_DAYS = 14

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


def _rn_qc_flag(item: dict[str, Any]) -> int | None:
    """강수 QC — API는 ``rnQcflg``, 문서/샘플은 ``rnQcflag``."""
    for key in ("rnQcflg", "rnQcflag"):
        qc = parse_qc_flag(item.get(key))
        if qc is not None:
            return qc
    return None


def parse_rn_observation(item: dict[str, Any]) -> float | None:
    """시간당 강수량(mm) — None이면 해당 시각 강수 행을 생략한다.

    공공데이터 ``getWthrDataList`` 응답 필드:
    - ``rn``: 시간당 강수량(mm)
    - ``rnQcflg``: 품질검사 (0=정상, 1=오류, 9=결측). 샘플·문서의 ``rnQcflag`` 도 수용.

    **2026-07-05 서울(108) raw 검증 근거** (``data/raw/ground_truth_asos/20260705_seoul.json``):
    - 무강수 대부분: ``rn=""``, ``rnQcflg=""`` (QC 미표기) — 기온(``ta``)은 정상.
      이를 생략하면 무강수 시각만 빠져 **강수 빈도가 과대 추정**된다.
    - ``rn="0.0"``, ``rnQcflg=""`` — 명시적 무강수 (기존에도 저장됨).
    - ``rn=""``, ``rnQcflg="9"`` — 결측(00·13·15·16·20시). 0.0 대체 금지.

    규칙:
    - ``rnQcflg==0``: 빈 ``rn`` → 0.0, 숫자면 그대로.
    - ``rnQcflg`` in (1, 9): 행 생략.
    - ``rnQcflg`` 없음/빈값: ``rn`` 숫자면 사용; 빈 ``rn`` 이면 ``ta`` 가 있을 때 무강수 0.0.
    """
    qc = _rn_qc_flag(item)
    rn_raw = item.get("rn")

    if qc is not None:
        if qc == 0:
            value = parse_optional_float(rn_raw)
            return 0.0 if value is None else value
        return None

    value = parse_optional_float(rn_raw)
    if value is not None:
        return value

    if parse_optional_float(item.get("ta")) is not None:
        return 0.0

    return None


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


def day_window_kst(obs_date: date) -> tuple[datetime, datetime]:
    """관측일(KST) 00~23시."""
    start_kst = datetime(obs_date.year, obs_date.month, obs_date.day, 0, 0, tzinfo=KST)
    end_kst = datetime(obs_date.year, obs_date.month, obs_date.day, 23, 0, tzinfo=KST)
    return start_kst, end_kst


def backfill_obs_dates(
    *,
    now: datetime | None = None,
    days: int = BACKFILL_DAYS,
) -> list[date]:
    """D-1 … D-N 관측일 (KST). ``days`` = 소급 일수."""
    now_kst = (now or datetime.now(KST)).astimezone(KST)
    return [now_kst.date() - timedelta(days=offset) for offset in range(1, days + 1)]


def missing_backfill_obs_dates(
    data_dir: Path,
    *,
    now: datetime | None = None,
    days: int = BACKFILL_DAYS,
    source: str = SOURCE_GROUND_TRUTH_ASOS,
) -> list[date]:
    """parquet 에 없는 관측일만 반환 (API 호출 대상)."""
    candidates = backfill_obs_dates(now=now, days=days)
    stored = load_stored_issue_dates(data_dir, candidates, source=source)
    return [obs_date for obs_date in candidates if obs_date not in stored]


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


def collect_asos_obs_date(
    obs_date: date,
    *,
    station: str = STATION_SEOUL,
    data_dir: Path = DATA_DIR,
    session: requests.Session | None = None,
    decoding_key: str | None = None,
    encoding_key: str | None = None,
) -> tuple[pd.DataFrame, Path, Path]:
    """단일 관측일(KST) ASOS 시간자료 수집·저장."""
    dec, enc = load_keys()
    if decoding_key is not None:
        dec = decoding_key
    if encoding_key is not None:
        enc = encoding_key
    if not dec and not enc:
        raise AsosFetchError("공공데이터 API 키가 설정되지 않았습니다.")

    start_kst, end_kst = day_window_kst(obs_date)
    sess = session or requests.Session()
    items = fetch_asos_hourly_all_pages(
        start_kst,
        end_kst,
        sess,
        decoding_key=dec,
        encoding_key=enc,
    )
    frame = parse_asos_items_to_long(items, station=station)
    if frame.empty:
        raise AsosFetchError(f"유효한 ASOS 관측 행이 없습니다: {obs_date}")

    raw_path = save_raw_json(
        {"items": items, "start_kst": start_kst.isoformat(), "end_kst": end_kst.isoformat()},
        asos_raw_json_path(data_dir, obs_date=obs_date, station=station),
    )

    staged = stage_asos_for_storage(frame)
    parquet_path = upsert_parquet(
        staged,
        data_dir=data_dir,
        issue_date=obs_date,
        source=SOURCE_GROUND_TRUTH_ASOS,
    )
    return frame, raw_path, parquet_path


def collect_asos_daily(
    *,
    station: str = STATION_SEOUL,
    data_dir: Path = DATA_DIR,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[Path]:
    """전일(D-1) ASOS 시간자료 적재 — source=ground_truth_asos 파티션."""
    now_kst = (now or datetime.now(KST)).astimezone(KST)
    obs_date = (now_kst - timedelta(days=1)).date()
    frame, raw_path, parquet_path = collect_asos_obs_date(
        obs_date,
        station=station,
        data_dir=data_dir,
        session=session,
    )

    print("=== ASOS 실측 적재 완료 ===")
    print(f"관측일(KST) : {obs_date}")
    print(f"행 수       : {len(frame)}")
    print(f"raw JSON    : {raw_path}")
    print(f"parquet     : {parquet_path}")
    return [parquet_path]


def run_backfill(
    *,
    data_dir: Path = DATA_DIR,
    now: datetime | None = None,
    days: int = BACKFILL_DAYS,
) -> int:
    decoding, encoding = load_keys()
    if not decoding and not encoding:
        print("[error] 공공데이터 API 키가 설정되지 않았습니다.", file=sys.stderr)
        return 1

    candidates = backfill_obs_dates(now=now, days=days)
    missing = missing_backfill_obs_dates(data_dir, now=now, days=days)
    skipped_stored = len(candidates) - len(missing)

    if not missing:
        print("=== ASOS backfill — 수집 대상 없음 ===")
        print(f"후보 일수   : {len(candidates)} (D-1 … D-{days})")
        print(f"이미 저장됨 : {skipped_stored}")
        return 0

    session = requests.Session()
    collected = 0
    errors = 0
    for i, obs_date in enumerate(sorted(missing)):
        if i > 0:
            time.sleep(REQUEST_INTERVAL_SEC)
        try:
            frame, _, parquet_path = collect_asos_obs_date(
                obs_date,
                data_dir=data_dir,
                session=session,
                decoding_key=decoding,
                encoding_key=encoding,
            )
            collected += 1
            print(f"  ✓ {obs_date}  rows={len(frame)}  → {parquet_path}")
        except (AsosFetchError, RuntimeError) as exc:
            errors += 1
            print(f"  ✗ {obs_date}  {exc}", file=sys.stderr)

    print()
    print("=== ASOS backfill 완료 ===")
    print(f"수집 성공  : {collected}")
    print(f"스킵(저장됨): {skipped_stored}")
    print(f"실패       : {errors}")
    return 1 if errors else 0


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
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="D-1 … D-N 관측일 소급 적재 (이미 저장된 날짜 스킵)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=BACKFILL_DAYS,
        help=f"소급 일수 (기본: {BACKFILL_DAYS}, D-1부터)",
    )
    args = parser.parse_args(argv)

    if args.collect:
        return run_collect()
    if args.backfill:
        return run_backfill(days=args.days)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
