"""기상청 단기예보 적재 — API 호출, 파싱, parquet/raw 저장."""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from src.schema import (
    KMA_CATEGORY_TO_VARIABLE,
    SOURCE_KMA_VILAGE,
    STANDARD_COLUMNS,
    STATION_SEOUL,
    validate_standard_frame,
)
from src.sources.kma_auth import (
    BASE_TIMES,
    KST,
    REQUEST_INTERVAL_SEC,
    SEOUL_NX,
    SEOUL_NY,
    fetch_vilage_fcst,
    load_keys,
    parse_api_payload,
    pick_base_time,
    vilage_fcst_params,
)
from src.sources.store import (
    DATA_DIR,
    attach_issue_time,
    load_stored_issue_times,
    raw_json_path,
    save_raw_json,
    upsert_parquet,
)

BACKFILL_DAYS = 2
_COLLECTED_CATEGORIES = frozenset(KMA_CATEGORY_TO_VARIABLE)


class KMACollectError(RuntimeError):
    """적재 파이프라인 오류."""


def parse_issue_time(base_date: str, base_time: str) -> datetime:
    """발표시각(base_date/base_time, KST) → UTC."""
    issue_kst = datetime.strptime(f"{base_date}{base_time}", "%Y%m%d%H%M").replace(tzinfo=KST)
    return issue_kst.astimezone(timezone.utc)


def parse_fcst_time(fcst_date: str, fcst_time: str) -> datetime:
    """유효시각(fcstDate/fcstTime, KST) → UTC."""
    fcst_kst = datetime.strptime(f"{fcst_date}{fcst_time}", "%Y%m%d%H%M").replace(tzinfo=KST)
    return fcst_kst.astimezone(timezone.utc)


def compute_lead_time_h(issue_time: datetime, valid_time: datetime) -> int:
    """valid_time - issue_time 을 정수 시간으로."""
    delta = valid_time - issue_time
    return int(delta.total_seconds() // 3600)


def parse_pcp_value(raw: str) -> float | None:
    """강수량(PCP) fcstValue 파싱."""
    text = raw.strip()
    if not text or text in {"-", "강수없음"}:
        return 0.0
    if "미만" in text:
        return 0.0
    if text.isdigit():
        return float(text)
    match = re.match(r"^(\d+(?:\.\d+)?)", text)
    if match:
        return float(match.group(1))
    return None


def parse_fcst_value(category: str, raw: str) -> float | None:
    if category == "PCP":
        return parse_pcp_value(raw)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_vilage_fcst_payload(
    payload: dict[str, Any],
    *,
    station: str = STATION_SEOUL,
    issue_time: datetime | None = None,
) -> pd.DataFrame:
    """getVilageFcst JSON → 표준 long-format (6컬럼)."""
    code, msg, items = parse_api_payload(payload)
    if code != "00":
        raise ValueError(f"resultCode={code} msg={msg}")

    if not items:
        return pd.DataFrame(columns=list(STANDARD_COLUMNS))

    first = items[0]
    base_date = str(first.get("baseDate", ""))
    base_time = str(first.get("baseTime", ""))
    issue = issue_time or parse_issue_time(base_date, base_time)

    rows: list[dict[str, Any]] = []
    for item in items:
        category = str(item.get("category", ""))
        if category not in _COLLECTED_CATEGORIES:
            continue
        variable = KMA_CATEGORY_TO_VARIABLE[category]
        fcst_date = str(item.get("fcstDate", ""))
        fcst_time = str(item.get("fcstTime", ""))
        if not fcst_date or not fcst_time:
            continue
        value = parse_fcst_value(category, str(item.get("fcstValue", "")))
        if value is None:
            continue
        valid_time = parse_fcst_time(fcst_date, fcst_time)
        rows.append(
            {
                "station": station,
                "valid_time": valid_time,
                "lead_time_h": compute_lead_time_h(issue, valid_time),
                "variable": variable,
                "value": value,
                "source": SOURCE_KMA_VILAGE,
            }
        )

    if not rows:
        return pd.DataFrame(columns=list(STANDARD_COLUMNS))

    frame = pd.DataFrame(rows, columns=list(STANDARD_COLUMNS))
    validate_standard_frame(frame, "parse_vilage_fcst_payload")
    return frame


def iter_backfill_issue_slots(
    *,
    now: datetime | None = None,
    days_back: int = BACKFILL_DAYS,
) -> Iterator[tuple[str, str, datetime]]:
    """오늘~days_back 일 전 × 8개 base_time 후보 (KST 발표시각)."""
    now_kst = (now or datetime.now(KST)).astimezone(KST)
    for day_offset in range(days_back + 1):
        base_day = now_kst.date() - timedelta(days=day_offset)
        base_date = base_day.strftime("%Y%m%d")
        for base_time in BASE_TIMES:
            issue_kst = datetime(
                base_day.year,
                base_day.month,
                base_day.day,
                int(base_time[:2]),
                int(base_time[2:]),
                tzinfo=KST,
            )
            if issue_kst > now_kst:
                continue
            yield base_date, base_time, issue_kst.astimezone(timezone.utc)


def backfill_issue_dates(
    *,
    now: datetime | None = None,
    days_back: int = BACKFILL_DAYS,
) -> list[date]:
    now_kst = (now or datetime.now(KST)).astimezone(KST)
    return [now_kst.date() - timedelta(days=offset) for offset in range(days_back + 1)]


def missing_backfill_slots(
    data_dir: Path,
    *,
    now: datetime | None = None,
    days_back: int = BACKFILL_DAYS,
    source: str = SOURCE_KMA_VILAGE,
    station: str = STATION_SEOUL,
) -> list[tuple[str, str, datetime]]:
    """parquet 에 없는 issue_time 슬롯만 반환 (API 호출 대상)."""
    dates = backfill_issue_dates(now=now, days_back=days_back)
    stored = load_stored_issue_times(data_dir, dates, source=source, station=station)
    missing: list[tuple[str, str, datetime]] = []
    for base_date, base_time, issue_utc in iter_backfill_issue_slots(now=now, days_back=days_back):
        if issue_utc in stored:
            continue
        missing.append((base_date, base_time, issue_utc))
    return missing


def collect_issue_forecast(
    base_date: str,
    base_time: str,
    *,
    station: str = STATION_SEOUL,
    nx: int = SEOUL_NX,
    ny: int = SEOUL_NY,
    data_dir: Path = DATA_DIR,
    session: requests.Session | None = None,
    decoding_key: str | None = None,
    encoding_key: str | None = None,
) -> tuple[pd.DataFrame, Path, Path]:
    """단일 (base_date, base_time) 발표 예보 수집·저장."""
    dec, enc = load_keys()
    if decoding_key is not None:
        dec = decoding_key
    if encoding_key is not None:
        enc = encoding_key
    if not dec and not enc:
        raise KMACollectError("KMA API 키가 설정되지 않았습니다. .env 를 확인하세요.")

    params = vilage_fcst_params(base_date, base_time, nx=nx, ny=ny)
    sess = session or requests.Session()
    payload = fetch_vilage_fcst(params, sess, decoding_key=dec, encoding_key=enc)

    issue_time = parse_issue_time(base_date, base_time)
    issue_date = issue_time.astimezone(KST).date()

    raw_path = save_raw_json(
        payload,
        raw_json_path(
            data_dir,
            issue_date=issue_date,
            base_time=base_time,
            station=station,
            source=SOURCE_KMA_VILAGE,
        ),
    )

    frame = parse_vilage_fcst_payload(payload, station=station, issue_time=issue_time)
    if frame.empty:
        raise KMACollectError(f"파싱 결과가 비어 있습니다: {base_date} {base_time}")

    staged = attach_issue_time(frame, issue_time)
    parquet_path = upsert_parquet(
        staged,
        data_dir=data_dir,
        issue_date=issue_date,
        source=SOURCE_KMA_VILAGE,
    )
    return frame, raw_path, parquet_path


def collect_latest_forecast(
    *,
    station: str = STATION_SEOUL,
    nx: int = SEOUL_NX,
    ny: int = SEOUL_NY,
    data_dir: Path = DATA_DIR,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> tuple[pd.DataFrame, Path, Path]:
    """최신 발표시각 단기예보 수집·저장."""
    now_kst = (now or datetime.now(KST)).astimezone(KST)
    base_date = now_kst.strftime("%Y%m%d")
    base_time = pick_base_time(now_kst.date(), now_kst)
    return collect_issue_forecast(
        base_date,
        base_time,
        station=station,
        nx=nx,
        ny=ny,
        data_dir=data_dir,
        session=session,
    )


def run_backfill(
    *,
    data_dir: Path = DATA_DIR,
    now: datetime | None = None,
    days_back: int = BACKFILL_DAYS,
) -> int:
    decoding, encoding = load_keys()
    if not decoding and not encoding:
        print("[error] KMA API 키가 설정되지 않았습니다.", file=sys.stderr)
        return 1

    slots = list(iter_backfill_issue_slots(now=now, days_back=days_back))
    missing = missing_backfill_slots(data_dir, now=now, days_back=days_back)
    total_candidates = (days_back + 1) * len(BASE_TIMES)
    skipped_future = total_candidates - len(slots)
    skipped_stored = len(slots) - len(missing)

    if not missing:
        print("=== KMA backfill — 수집 대상 없음 ===")
        print(f"후보 슬롯   : {len(slots)} (미래 발표 제외)")
        print(f"이미 저장됨 : {skipped_stored}")
        return 0

    session = requests.Session()
    collected = 0
    errors = 0
    for i, (base_date, base_time, _) in enumerate(missing):
        if i > 0:
            time.sleep(REQUEST_INTERVAL_SEC)
        try:
            frame, raw_path, parquet_path = collect_issue_forecast(
                base_date,
                base_time,
                data_dir=data_dir,
                session=session,
                decoding_key=decoding,
                encoding_key=encoding,
            )
            collected += 1
            print(f"  ✓ {base_date} {base_time}  rows={len(frame)}  → {parquet_path.name}")
        except (KMACollectError, RuntimeError) as exc:
            errors += 1
            print(f"  ✗ {base_date} {base_time}  {exc}", file=sys.stderr)

    print()
    print("=== KMA backfill 완료 ===")
    print(f"수집 성공  : {collected}")
    print(f"스킵(저장됨): {skipped_stored}")
    print(f"스킵(미래) : {skipped_future}")
    print(f"실패       : {errors}")
    return 1 if errors else 0


def run_collect() -> int:
    try:
        frame, raw_path, parquet_path = collect_latest_forecast()
    except KMACollectError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    print("=== KMA 단기예보 적재 완료 ===")
    print(f"행 수       : {len(frame)}")
    print(f"변수        : {sorted(frame['variable'].unique())}")
    print(f"raw JSON    : {raw_path}")
    print(f"parquet     : {parquet_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="기상청 단기예보 적재")
    parser.add_argument("--collect", action="store_true", help="최신 발표 예보 수집·저장")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="오늘~2일 전 8개 base_time 중 미저장 슬롯 소급 수집",
    )
    args = parser.parse_args(argv)

    if args.collect:
        return run_collect()
    if args.backfill:
        return run_backfill()

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
