"""KMA 단기예보 파싱·저장 단위 테스트 (네트워크 없음)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from src.schema import (
    SOURCE_KMA_VILAGE,
    VARIABLE_PCP,
    VARIABLE_POP,
    VARIABLE_TEMPERATURE,
)
from src.sources.kma import (
    UPSERT_KEYS,
    attach_issue_time,
    compute_lead_time_h,
    parse_fcst_time,
    parse_issue_time,
    parse_pcp_value,
    parse_vilage_fcst_payload,
    upsert_parquet,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "kma_vilage_fcst_sample.json"
ISSUE_TIME = parse_issue_time("20260703", "1100")  # 2026-07-03 11:00 KST → UTC


@pytest.fixture
def sample_payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_parse_issue_and_fcst_time_utc():
    issue = parse_issue_time("20260703", "1100")
    assert issue == datetime(2026, 7, 3, 2, 0, tzinfo=timezone.utc)

    valid = parse_fcst_time("20260703", "1200")
    assert valid == datetime(2026, 7, 3, 3, 0, tzinfo=timezone.utc)
    assert compute_lead_time_h(issue, valid) == 1


def test_parse_pcp_values():
    assert parse_pcp_value("강수없음") == 0.0
    assert parse_pcp_value("1mm 미만") == 0.0
    assert parse_pcp_value("5") == 5.0


def test_parse_vilage_fcst_payload_standard_columns(sample_payload):
    frame = parse_vilage_fcst_payload(sample_payload, issue_time=ISSUE_TIME)

    assert len(frame) == 6
    assert set(frame["variable"]) == {VARIABLE_TEMPERATURE, VARIABLE_POP, VARIABLE_PCP}
    assert (frame["source"] == SOURCE_KMA_VILAGE).all()
    assert frame["valid_time"].dt.tz is not None

    tmp_12 = frame[
        (frame["variable"] == VARIABLE_TEMPERATURE)
        & (frame["valid_time"] == parse_fcst_time("20260703", "1200"))
    ].iloc[0]
    assert tmp_12["value"] == 25.0
    assert tmp_12["lead_time_h"] == 1

    pop_15 = frame[
        (frame["variable"] == VARIABLE_POP)
        & (frame["valid_time"] == parse_fcst_time("20260703", "1500"))
    ].iloc[0]
    assert pop_15["value"] == 40.0
    assert pop_15["lead_time_h"] == 4

    pcp_12 = frame[
        (frame["variable"] == VARIABLE_PCP)
        & (frame["valid_time"] == parse_fcst_time("20260703", "1200"))
    ].iloc[0]
    assert pcp_12["value"] == 0.0


def test_upsert_parquet_idempotent(tmp_path):
    frame = parse_vilage_fcst_payload(
        json.loads(FIXTURE_PATH.read_text(encoding="utf-8")), issue_time=ISSUE_TIME
    )
    staged = attach_issue_time(frame, ISSUE_TIME)
    issue_date = date(2026, 7, 3)

    path1 = upsert_parquet(staged, data_dir=tmp_path, issue_date=issue_date)
    path2 = upsert_parquet(staged, data_dir=tmp_path, issue_date=issue_date)

    assert path1 == path2
    stored = pd.read_parquet(path1)
    assert len(stored) == len(frame)
    assert list(UPSERT_KEYS) == ["source", "issue_time", "station", "valid_time", "variable"]
    assert stored.drop_duplicates(subset=list(UPSERT_KEYS)).shape[0] == len(stored)


def test_upsert_parquet_updates_duplicate_key(tmp_path):
    frame = parse_vilage_fcst_payload(
        json.loads(FIXTURE_PATH.read_text(encoding="utf-8")), issue_time=ISSUE_TIME
    )
    staged = attach_issue_time(frame, ISSUE_TIME)
    issue_date = date(2026, 7, 3)

    upsert_parquet(staged, data_dir=tmp_path, issue_date=issue_date)

    revised = staged.copy()
    mask = (revised["variable"] == VARIABLE_TEMPERATURE) & (revised["lead_time_h"] == 1)
    revised.loc[mask, "value"] = 99.0
    upsert_parquet(revised, data_dir=tmp_path, issue_date=issue_date)

    stored = pd.read_parquet(tmp_path / "parquet" / "issue_date=2026-07-03" / "forecasts.parquet")
    row = stored[(stored["variable"] == VARIABLE_TEMPERATURE) & (stored["lead_time_h"] == 1)].iloc[
        0
    ]
    assert row["value"] == 99.0
    assert len(stored) == len(frame)
