"""Open-Meteo 파싱·적재 단위 테스트 (네트워크 없음)."""

from __future__ import annotations

import json
from datetime import date, timezone
from pathlib import Path

import pandas as pd
import pytest
from src.schema import (
    SOURCE_OPENMETEO_ECMWF,
    SOURCE_OPENMETEO_GFS,
    VARIABLE_POP,
    VARIABLE_TEMPERATURE,
)
from src.sources.openmeteo import (
    _legacy_forecast_entries,
    _legacy_forecasts_to_long,
    approximate_legacy_issue_time,
    attach_legacy_forecast_issue_times,
    collect_openmeteo_forward,
    collect_openmeteo_legacy_previous_runs,
    compute_forward_lead_time_h,
    days_ahead_from_lead,
    parse_forward_hourly_to_long,
    partition_frames_by_source_issue_date,
    truncate_issue_time,
)

LEGACY_FIXTURE = Path(__file__).parent / "fixtures" / "openmeteo_hourly_sample.json"
FORWARD_FIXTURE = Path(__file__).parent / "fixtures" / "openmeteo_forward_hourly_sample.json"


@pytest.fixture
def sample_legacy_hourly() -> dict:
    return json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))["hourly"]


@pytest.fixture
def sample_forward_hourly() -> dict:
    return json.loads(FORWARD_FIXTURE.read_text(encoding="utf-8"))["hourly"]


@pytest.fixture
def forward_issue_time() -> pd.Timestamp:
    return pd.Timestamp("2026-07-03T12:00", tz="UTC")


# --- legacy Previous Runs ---


def test_legacy_forecast_entries_multi_model():
    entries = _legacy_forecast_entries(("ecmwf_ifs", "gfs_global"))
    sources = {e[2] for e in entries}
    variables = {e[3] for e in entries}
    assert sources == {SOURCE_OPENMETEO_ECMWF, SOURCE_OPENMETEO_GFS}
    assert variables == {VARIABLE_TEMPERATURE, VARIABLE_POP}
    assert any("ecmwf_ifs" in e[0] for e in entries)


def test_legacy_forecasts_to_long_pop_percent_unchanged(sample_legacy_hourly):
    entries = _legacy_forecast_entries(("ecmwf_ifs", "gfs_global"))
    frame = _legacy_forecasts_to_long(sample_legacy_hourly, station="seoul", entries=entries)

    pop = frame[frame["variable"] == VARIABLE_POP]
    assert not pop.empty
    assert pop["value"].between(0, 100).all()


def test_legacy_collect_writes_parquet(tmp_path, monkeypatch, sample_legacy_hourly):
    def fake_fetch(**_kwargs):
        return sample_legacy_hourly

    monkeypatch.setattr(
        "src.sources.openmeteo._fetch_legacy_previous_runs_payload",
        fake_fetch,
    )

    paths = collect_openmeteo_legacy_previous_runs(
        data_dir=tmp_path,
        now=pd.Timestamp("2026-06-02 00:00", tz="UTC").to_pydatetime(),
    )
    assert paths
    assert (tmp_path / "raw" / "openmeteo").is_dir()


def test_days_ahead_from_lead():
    assert days_ahead_from_lead(24) == 1
    assert days_ahead_from_lead(48) == 2


def test_approximate_legacy_issue_time_uses_utc_cycle():
    valid = pd.Timestamp("2026-06-01 14:00", tz="UTC")
    issue = approximate_legacy_issue_time(valid, days_ahead=1)
    assert issue == pd.Timestamp("2026-05-31 12:00", tz="UTC").to_pydatetime().replace(
        tzinfo=timezone.utc
    )


def test_legacy_attach_and_partition(sample_legacy_hourly):
    entries = _legacy_forecast_entries(("ecmwf_ifs", "gfs_global"))
    frame = _legacy_forecasts_to_long(sample_legacy_hourly, station="seoul", entries=entries)
    staged = attach_legacy_forecast_issue_times(frame)
    parts = partition_frames_by_source_issue_date(staged)
    assert (SOURCE_OPENMETEO_ECMWF, date(2026, 5, 31)) in parts


# --- forward Forecast API ---


def test_truncate_issue_time_to_hour():
    raw = pd.Timestamp("2026-07-03 14:37:22", tz="UTC").to_pydatetime()
    assert truncate_issue_time(raw) == pd.Timestamp("2026-07-03 14:00", tz="UTC").to_pydatetime()


def test_compute_forward_lead_time_h(forward_issue_time):
    valid = pd.Timestamp("2026-07-03T15:00", tz="UTC")
    assert compute_forward_lead_time_h(forward_issue_time.to_pydatetime(), valid) == 3


def test_parse_forward_excludes_lead_lt_one(sample_forward_hourly, forward_issue_time):
    frame = parse_forward_hourly_to_long(
        sample_forward_hourly,
        issue_time=forward_issue_time.to_pydatetime(),
    )
    assert (frame["lead_time_h"] >= 1).all()
    # issue 12:00 → valid 11:00 (lead 0) 제외
    assert not (frame["valid_time"] == pd.Timestamp("2026-07-03T11:00", tz="UTC")).any()


def test_parse_forward_pop_percent_unchanged(sample_forward_hourly, forward_issue_time):
    frame = parse_forward_hourly_to_long(
        sample_forward_hourly,
        issue_time=forward_issue_time.to_pydatetime(),
    )
    pop = frame[frame["variable"] == VARIABLE_POP]
    assert not pop.empty
    assert pop["value"].between(0, 100).all()
    ecmwf = pop[pop["source"] == SOURCE_OPENMETEO_ECMWF].sort_values("valid_time")
    assert ecmwf.iloc[0]["value"] == pytest.approx(30.0)
    assert ecmwf.iloc[0]["lead_time_h"] == 1
    assert ecmwf.iloc[0]["valid_time"] == pd.Timestamp("2026-07-03T13:00", tz="UTC")


def test_parse_forward_lead_time_from_valid_minus_issue(sample_forward_hourly, forward_issue_time):
    frame = parse_forward_hourly_to_long(
        sample_forward_hourly,
        issue_time=forward_issue_time.to_pydatetime(),
    )
    row = frame[
        (frame["source"] == SOURCE_OPENMETEO_GFS)
        & (frame["valid_time"] == pd.Timestamp("2026-07-03T14:00", tz="UTC"))
        & (frame["variable"] == VARIABLE_TEMPERATURE)
    ].iloc[0]
    assert row["lead_time_h"] == 2


def test_collect_forward_writes_parquet(
    tmp_path, monkeypatch, sample_forward_hourly, forward_issue_time
):
    def fake_fetch(**_kwargs):
        return sample_forward_hourly

    monkeypatch.setattr("src.sources.openmeteo._fetch_forward_hourly_payload", fake_fetch)

    paths = collect_openmeteo_forward(
        data_dir=tmp_path,
        now=forward_issue_time.to_pydatetime(),
    )
    assert len(paths) == 2
    stored = pd.read_parquet(paths[0])
    assert "issue_time" in stored.columns
    assert set(stored["variable"]) == {VARIABLE_TEMPERATURE, VARIABLE_POP}
    assert stored["issue_time"].nunique() == 1
    assert pd.Timestamp(stored["issue_time"].iloc[0]).date() == date(2026, 7, 3)
