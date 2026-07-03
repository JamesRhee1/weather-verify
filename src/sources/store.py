"""parquet/raw 영속 저장 공통 유틸."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from src.schema import SOURCE_KMA_VILAGE, STATION_SEOUL
from src.sources.kma_auth import ROOT

DATA_DIR = ROOT / "data"
UPSERT_KEYS = ("source", "issue_time", "station", "valid_time", "variable")


def parquet_partition_path(data_dir: Path, issue_date: date, source: str) -> Path:
    return (
        data_dir
        / "parquet"
        / f"source={source}"
        / f"issue_date={issue_date.isoformat()}"
        / "forecasts.parquet"
    )


def raw_json_path(
    data_dir: Path,
    *,
    issue_date: date,
    base_time: str,
    station: str,
    source: str = SOURCE_KMA_VILAGE,
) -> Path:
    subdir = "raw" if source == SOURCE_KMA_VILAGE else f"raw/{source}"
    return data_dir / subdir / f"{issue_date.strftime('%Y%m%d')}_{base_time}_{station}.json"


def asos_raw_json_path(data_dir: Path, *, obs_date: date, station: str) -> Path:
    return data_dir / "raw" / "ground_truth_asos" / f"{obs_date.strftime('%Y%m%d')}_{station}.json"


def attach_issue_time(frame: pd.DataFrame, issue_time: datetime) -> pd.DataFrame:
    """저장·멱등 upsert 용 issue_time 컬럼 부착 (표준 6컬럼 외)."""
    out = frame.copy()
    out["issue_time"] = issue_time
    return out


def save_raw_json(
    payload: dict[str, Any],
    path: Path,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def upsert_parquet(staged: pd.DataFrame, *, data_dir: Path, issue_date: date, source: str) -> Path:
    """발표일·source 파티션에 멱등 upsert."""
    path = parquet_partition_path(data_dir, issue_date, source)
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


def load_stored_issue_times(
    data_dir: Path,
    issue_dates: list[date],
    *,
    source: str,
    station: str = STATION_SEOUL,
) -> set[datetime]:
    """파티션 parquet 에서 이미 저장된 (source, station) issue_time 집합."""
    stored: set[datetime] = set()
    for issue_date in issue_dates:
        path = parquet_partition_path(data_dir, issue_date, source)
        if not path.is_file():
            continue
        frame = pd.read_parquet(path)
        if frame.empty or "issue_time" not in frame.columns:
            continue
        subset = frame
        if "source" in frame.columns:
            subset = subset[subset["source"] == source]
        if "station" in frame.columns:
            subset = subset[subset["station"] == station]
        for ts in pd.to_datetime(subset["issue_time"], utc=True).drop_duplicates():
            stored.add(ts.to_pydatetime())
    return stored
