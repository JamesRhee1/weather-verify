"""KMA backfill 슬롯 판단 단위 테스트 (네트워크 없음)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from src.schema import SOURCE_KMA_VILAGE
from src.sources.kma import (
    BACKFILL_DAYS,
    backfill_issue_dates,
    iter_backfill_issue_slots,
    missing_backfill_slots,
    parse_issue_time,
    parse_vilage_fcst_payload,
)
from src.sources.kma_auth import BASE_TIMES, KST
from src.sources.store import attach_issue_time, upsert_parquet

FIXTURE = Path(__file__).parent / "fixtures" / "kma_vilage_fcst_sample.json"
NOW_KST = datetime(2026, 7, 3, 14, 30, tzinfo=KST)  # 14:30 — 1700/2000/2300 미발표


def test_iter_backfill_skips_future_base_times_on_today():
    slots = list(iter_backfill_issue_slots(now=NOW_KST, days_back=BACKFILL_DAYS))
    today_times = {bt for bd, bt, _ in slots if bd == "20260703"}
    assert today_times == {"0200", "0500", "0800", "1100", "1400"}
    assert "1700" not in today_times


def test_iter_backfill_includes_all_eight_times_for_past_days():
    slots = list(iter_backfill_issue_slots(now=NOW_KST, days_back=BACKFILL_DAYS))
    for base_date in ("20260702", "20260701"):
        times = {bt for bd, bt, _ in slots if bd == base_date}
        assert times == set(BASE_TIMES)


def test_backfill_issue_dates_covers_three_days():
    dates = backfill_issue_dates(now=NOW_KST, days_back=BACKFILL_DAYS)
    assert dates == [date(2026, 7, 3), date(2026, 7, 2), date(2026, 7, 1)]


def test_missing_backfill_slots_excludes_stored_issue_time(tmp_path: Path):
    issue_time = parse_issue_time("20260703", "1100")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    frame = parse_vilage_fcst_payload(payload, issue_time=issue_time)
    staged = attach_issue_time(frame, issue_time)
    upsert_parquet(
        staged,
        data_dir=tmp_path,
        issue_date=date(2026, 7, 3),
        source=SOURCE_KMA_VILAGE,
    )

    missing = missing_backfill_slots(tmp_path, now=NOW_KST, days_back=BACKFILL_DAYS)
    missing_keys = {(bd, bt) for bd, bt, _ in missing}

    assert ("20260703", "1100") not in missing_keys
    assert ("20260703", "1700") not in missing_keys  # 미래 발표
    assert ("20260702", "1100") in missing_keys


def test_missing_backfill_slots_empty_when_all_published_stored(tmp_path: Path):
    """과거 2일 8슬롯 + 오늘 5슬롯 전부 저장 시 backfill 대상 없음."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for base_date, base_time in [
        ("20260701", "1100"),
        ("20260702", "1100"),
        ("20260703", "1100"),
    ]:
        issue_time = parse_issue_time(base_date, base_time)
        frame = parse_vilage_fcst_payload(payload, issue_time=issue_time)
        staged = attach_issue_time(frame, issue_time)
        upsert_parquet(
            staged,
            data_dir=tmp_path,
            issue_date=datetime.strptime(base_date, "%Y%m%d").date(),
            source=SOURCE_KMA_VILAGE,
        )

    missing = missing_backfill_slots(tmp_path, now=NOW_KST, days_back=BACKFILL_DAYS)
    stored_times = {parse_issue_time("20260703", "1100")}
    for _, bt, it in missing:
        assert it not in stored_times
    assert len(missing) > 0  # 나머지 슬롯은 여전히 누락
