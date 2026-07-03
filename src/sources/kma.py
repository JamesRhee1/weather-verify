"""기상청 단기예보 적재 — API 호출, 파싱, parquet/raw 저장."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
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
    KST,
    ROOT,
    SEOUL_NX,
    SEOUL_NY,
    fetch_vilage_fcst,
    load_keys,
    parse_api_payload,
    pick_base_time,
    vilage_fcst_params,
)

DATA_DIR = ROOT / "data"
UPSERT_KEYS = ("source", "issue_time", "station", "valid_time", "variable")
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


def attach_issue_time(frame: pd.DataFrame, issue_time: datetime) -> pd.DataFrame:
    """저장·멱등 upsert 용 issue_time 컬럼 부착 (표준 6컬럼 외)."""
    out = frame.copy()
    out["issue_time"] = issue_time
    return out


def raw_json_path(
    data_dir: Path,
    *,
    issue_date: date,
    base_time: str,
    station: str,
) -> Path:
    return data_dir / "raw" / f"{issue_date.strftime('%Y%m%d')}_{base_time}_{station}.json"


def parquet_partition_path(data_dir: Path, issue_date: date) -> Path:
    return data_dir / "parquet" / f"issue_date={issue_date.isoformat()}" / "forecasts.parquet"


def save_raw_json(
    payload: dict[str, Any],
    *,
    data_dir: Path,
    issue_date: date,
    base_time: str,
    station: str,
) -> Path:
    path = raw_json_path(data_dir, issue_date=issue_date, base_time=base_time, station=station)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def upsert_parquet(staged: pd.DataFrame, *, data_dir: Path, issue_date: date) -> Path:
    """발표일 파티션에 멱등 upsert."""
    path = parquet_partition_path(data_dir, issue_date)
    path.parent.mkdir(parents=True, exist_ok=True)

    for col in UPSERT_KEYS:
        if col not in staged.columns:
            raise ValueError(f"upsert 에 필요한 컬럼 누락: {col}")

    staged = staged.copy()
    staged["issue_time"] = pd.to_datetime(staged["issue_time"], utc=True)
    staged["valid_time"] = pd.to_datetime(staged["valid_time"], utc=True)

    if path.is_file():
        existing = pd.read_parquet(path)
        existing["issue_time"] = pd.to_datetime(existing["issue_time"], utc=True)
        existing["valid_time"] = pd.to_datetime(existing["valid_time"], utc=True)
        combined = pd.concat([existing, staged], ignore_index=True)
    else:
        combined = staged

    combined = combined.drop_duplicates(subset=list(UPSERT_KEYS), keep="last")
    combined = combined.sort_values(list(UPSERT_KEYS)).reset_index(drop=True)
    combined.to_parquet(path, index=False)
    return path


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
    decoding, encoding = load_keys()
    if not decoding and not encoding:
        raise KMACollectError("KMA API 키가 설정되지 않았습니다. .env 를 확인하세요.")

    now_kst = (now or datetime.now(KST)).astimezone(KST)
    base_date = now_kst.strftime("%Y%m%d")
    base_time = pick_base_time(now_kst.date(), now_kst)
    params = vilage_fcst_params(base_date, base_time, nx=nx, ny=ny)

    sess = session or requests.Session()
    payload = fetch_vilage_fcst(params, sess, decoding_key=decoding, encoding_key=encoding)

    issue_time = parse_issue_time(base_date, base_time)
    issue_date = issue_time.astimezone(KST).date()

    raw_path = save_raw_json(
        payload,
        data_dir=data_dir,
        issue_date=issue_date,
        base_time=base_time,
        station=station,
    )

    frame = parse_vilage_fcst_payload(payload, station=station, issue_time=issue_time)
    if frame.empty:
        raise KMACollectError("파싱 결과가 비어 있습니다.")

    staged = attach_issue_time(frame, issue_time)
    parquet_path = upsert_parquet(staged, data_dir=data_dir, issue_date=issue_date)
    return frame, raw_path, parquet_path


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
    args = parser.parse_args(argv)

    if args.collect:
        return run_collect()

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
