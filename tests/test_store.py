"""store.py upsert·방어 로직 단위 테스트."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from src.schema import SOURCE_KMA_VILAGE, VARIABLE_POP
from src.sources.store import attach_issue_time, drop_future_issue_rows, upsert_parquet

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
PAST_ISSUE = datetime(2026, 7, 3, 6, 0, tzinfo=timezone.utc)
FUTURE_ISSUE = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)


def _pop_row(*, issue_time: datetime) -> pd.DataFrame:
    row = {
        "station": "seoul",
        "valid_time": pd.Timestamp("2026-07-03T12:00", tz="UTC"),
        "lead_time_h": 6,
        "variable": VARIABLE_POP,
        "value": 40.0,
        "source": SOURCE_KMA_VILAGE,
    }
    return attach_issue_time(pd.DataFrame([row]), issue_time)


def test_drop_future_issue_rows():
    frame = _pop_row(issue_time=FUTURE_ISSUE)
    cleaned = drop_future_issue_rows(frame, now=NOW)
    assert cleaned.empty


def test_upsert_parquet_excludes_future_issue_time(tmp_path: Path):
    good = _pop_row(issue_time=PAST_ISSUE)
    bad = _pop_row(issue_time=FUTURE_ISSUE)
    staged = pd.concat([good, bad], ignore_index=True)

    path = upsert_parquet(
        staged,
        data_dir=tmp_path,
        issue_date=date(2026, 7, 3),
        source=SOURCE_KMA_VILAGE,
    )

    stored = pd.read_parquet(path)
    assert len(stored) == 1
    assert pd.Timestamp(stored["issue_time"].iloc[0]) == pd.Timestamp(PAST_ISSUE)
