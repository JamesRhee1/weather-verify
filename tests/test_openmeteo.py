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
    _forecast_entries,
    _forecasts_to_long,
    approximate_issue_time,
    attach_forecast_issue_times,
    collect_openmeteo_daily,
    days_ahead_from_lead,
    partition_frames_by_source_issue_date,
)

FIXTURE = Path(__file__).parent / "fixtures" / "openmeteo_hourly_sample.json"


@pytest.fixture
def sample_hourly() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["hourly"]


def test_forecast_entries_multi_model():
    entries = _forecast_entries(("ecmwf_ifs", "gfs_global"))
    sources = {e[2] for e in entries}
    variables = {e[3] for e in entries}
    assert sources == {SOURCE_OPENMETEO_ECMWF, SOURCE_OPENMETEO_GFS}
    assert variables == {VARIABLE_TEMPERATURE, VARIABLE_POP}
    assert any("ecmwf_ifs" in e[0] for e in entries)
    assert any("gfs_global" in e[0] for e in entries)


def test_forecasts_to_long_pop_percent_unchanged(sample_hourly):
    entries = _forecast_entries(("ecmwf_ifs", "gfs_global"))
    frame = _forecasts_to_long(sample_hourly, station="seoul", entries=entries)

    pop = frame[frame["variable"] == VARIABLE_POP]
    assert not pop.empty
    assert pop["value"].between(0, 100).all()

    ecmwf_day1 = pop[
        (pop["source"] == SOURCE_OPENMETEO_ECMWF) & (pop["lead_time_h"] == 24)
    ].sort_values("valid_time")
    assert ecmwf_day1.iloc[0]["value"] == pytest.approx(10.0)
    assert ecmwf_day1.iloc[1]["value"] == pytest.approx(45.0)

    gfs_day2 = pop[(pop["source"] == SOURCE_OPENMETEO_GFS) & (pop["lead_time_h"] == 48)]
    assert gfs_day2.iloc[0]["value"] == pytest.approx(15.0)


def test_forecasts_to_long_skips_null_temperature(sample_hourly):
    entries = _forecast_entries(("ecmwf_ifs", "gfs_global"))
    frame = _forecasts_to_long(sample_hourly, station="seoul", entries=entries)
    ecmwf_temp = frame[
        (frame["source"] == SOURCE_OPENMETEO_ECMWF)
        & (frame["variable"] == VARIABLE_TEMPERATURE)
        & (frame["lead_time_h"] == 24)
    ]
    assert len(ecmwf_temp) == 2


def test_days_ahead_from_lead():
    assert days_ahead_from_lead(24) == 1
    assert days_ahead_from_lead(48) == 2


def test_approximate_issue_time_uses_utc_cycle():
    valid = pd.Timestamp("2026-06-01 14:00", tz="UTC")
    issue = approximate_issue_time(valid, days_ahead=1)
    assert issue == pd.Timestamp("2026-05-31 12:00", tz="UTC").to_pydatetime().replace(
        tzinfo=timezone.utc
    )


def test_attach_and_partition_by_source_issue_date(sample_hourly):
    entries = _forecast_entries(("ecmwf_ifs", "gfs_global"))
    frame = _forecasts_to_long(sample_hourly, station="seoul", entries=entries)
    staged = attach_forecast_issue_times(frame)
    assert "issue_time" in staged.columns

    parts = partition_frames_by_source_issue_date(staged)
    assert (SOURCE_OPENMETEO_ECMWF, date(2026, 5, 31)) in parts
    assert parts[(SOURCE_OPENMETEO_ECMWF, date(2026, 5, 31))]["source"].nunique() == 1


def test_collect_openmeteo_daily_writes_parquet(tmp_path, monkeypatch, sample_hourly):
    def fake_fetch(**_kwargs):
        return sample_hourly

    monkeypatch.setattr(
        "src.sources.openmeteo._fetch_hourly_payload",
        fake_fetch,
    )

    paths = collect_openmeteo_daily(
        data_dir=tmp_path,
        now=pd.Timestamp("2026-06-02 00:00", tz="UTC").to_pydatetime(),
    )
    assert paths
    stored = pd.read_parquet(paths[0])
    assert "issue_time" in stored.columns
    assert set(stored["variable"]) == {VARIABLE_TEMPERATURE, VARIABLE_POP}
    assert (tmp_path / "raw" / "openmeteo").is_dir()
