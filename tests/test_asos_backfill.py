"""ASOS backfill 슬롯 판단 단위 테스트 (네트워크 없음)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from src.schema import SOURCE_GROUND_TRUTH_ASOS, VARIABLE_TEMPERATURE
from src.sources.asos import (
    BACKFILL_DAYS,
    backfill_obs_dates,
    missing_backfill_obs_dates,
)
from src.sources.store import attach_issue_time, upsert_parquet

KST = ZoneInfo("Asia/Seoul")
NOW_KST = datetime(2026, 7, 6, 12, 0, tzinfo=KST)


def test_backfill_obs_dates_covers_d1_through_dN():
    dates = backfill_obs_dates(now=NOW_KST, days=BACKFILL_DAYS)
    assert len(dates) == BACKFILL_DAYS
    assert dates[0] == date(2026, 7, 5)
    assert dates[-1] == date(2026, 6, 22)


def test_missing_backfill_obs_dates_excludes_stored(tmp_path):
    obs_date = date(2026, 7, 5)
    row = {
        "station": "seoul",
        "valid_time": pd.Timestamp("2026-07-05T03:00", tz="UTC"),
        "lead_time_h": 0,
        "variable": VARIABLE_TEMPERATURE,
        "value": 20.0,
        "source": SOURCE_GROUND_TRUTH_ASOS,
    }
    frame = attach_issue_time(pd.DataFrame([row]), pd.Timestamp("2026-07-05T03:00", tz="UTC"))
    upsert_parquet(
        frame,
        data_dir=tmp_path,
        issue_date=obs_date,
        source=SOURCE_GROUND_TRUTH_ASOS,
    )

    missing = missing_backfill_obs_dates(tmp_path, now=NOW_KST, days=3)
    assert obs_date not in missing
    assert date(2026, 7, 4) in missing


def test_missing_backfill_obs_dates_empty_when_all_stored(tmp_path):
    for offset in (1, 2, 3):
        obs_date = NOW_KST.date() - timedelta(days=offset)
        row = {
            "station": "seoul",
            "valid_time": pd.Timestamp(f"{obs_date}T03:00", tz="UTC"),
            "lead_time_h": 0,
            "variable": VARIABLE_TEMPERATURE,
            "value": 20.0,
            "source": SOURCE_GROUND_TRUTH_ASOS,
        }
        frame = attach_issue_time(
            pd.DataFrame([row]),
            pd.Timestamp(f"{obs_date}T03:00", tz="UTC"),
        )
        upsert_parquet(
            frame,
            data_dir=tmp_path,
            issue_date=obs_date,
            source=SOURCE_GROUND_TRUTH_ASOS,
        )

    missing = missing_backfill_obs_dates(tmp_path, now=NOW_KST, days=3)
    assert missing == []
