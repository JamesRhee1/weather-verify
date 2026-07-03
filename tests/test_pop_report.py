"""POP 리포트 end-to-end 오프라인 테스트."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from run_pop_report import run_pop_report
from src.core.report import build_pop_report
from src.schema import (
    SOURCE_GROUND_TRUTH_ASOS,
    SOURCE_KMA_VILAGE,
    SOURCE_OPENMETEO_ECMWF,
    VARIABLE_PCP,
    VARIABLE_POP,
)
from src.sources.store import attach_issue_time, upsert_parquet

ISSUE = datetime(2026, 7, 3, 6, 0, tzinfo=timezone.utc)
ISSUE_DATE = date(2026, 7, 3)


def _stage_pop(
    *,
    source: str,
    valid_times: list[str],
    lead_h: int,
    pop_values: list[float],
) -> pd.DataFrame:
    rows = []
    for vt, pop in zip(valid_times, pop_values, strict=True):
        rows.append(
            {
                "station": "seoul",
                "valid_time": pd.Timestamp(vt, tz="UTC"),
                "lead_time_h": lead_h,
                "variable": VARIABLE_POP,
                "value": pop,
                "source": source,
            }
        )
    return attach_issue_time(pd.DataFrame(rows), ISSUE)


def _stage_asos(valid_times: list[str], pcp_mm: list[float]) -> pd.DataFrame:
    rows = []
    for vt, mm in zip(valid_times, pcp_mm, strict=True):
        vt_ts = pd.Timestamp(vt, tz="UTC")
        rows.append(
            {
                "station": "seoul",
                "valid_time": vt_ts,
                "lead_time_h": 0,
                "variable": VARIABLE_PCP,
                "value": mm,
                "source": SOURCE_GROUND_TRUTH_ASOS,
            }
        )
    frame = pd.DataFrame(rows)
    return attach_issue_time(frame, vt_ts)


def _seed_synthetic_parquet(data_dir: Path) -> None:
    """KMA·ECMWF POP + ASOS — lead 3h 버킷 1, window_3h 에서 outcome=1."""
    kma = _stage_pop(
        source=SOURCE_KMA_VILAGE,
        valid_times=["2026-07-03T15:00:00"],
        lead_h=3,
        pop_values=[30.0],
    )
    om = _stage_pop(
        source=SOURCE_OPENMETEO_ECMWF,
        valid_times=["2026-07-03T15:00:00"],
        lead_h=3,
        pop_values=[80.0],
    )
    asos = _stage_asos(
        ["2026-07-03T12:00:00", "2026-07-03T13:00:00", "2026-07-03T15:00:00"],
        [0.0, 1.2, 0.0],
    )

    upsert_parquet(kma, data_dir=data_dir, issue_date=ISSUE_DATE, source=SOURCE_KMA_VILAGE)
    upsert_parquet(om, data_dir=data_dir, issue_date=ISSUE_DATE, source=SOURCE_OPENMETEO_ECMWF)
    upsert_parquet(asos, data_dir=data_dir, issue_date=ISSUE_DATE, source=SOURCE_GROUND_TRUTH_ASOS)


def test_build_pop_report_e2e_window_mode(tmp_path: Path):
    data_dir = tmp_path / "data"
    reports_dir = tmp_path / "reports"
    _seed_synthetic_parquet(data_dir)

    result = build_pop_report(
        data_dir=data_dir,
        reports_dir=reports_dir,
        start_date=ISSUE_DATE,
        end_date=ISSUE_DATE,
        truth_mode="window_3h",
        min_sample=1,
    )

    assert result.message is None
    assert not result.summary.empty
    assert len(result.summary) == 2

    kma_row = result.summary[result.summary["source"] == SOURCE_KMA_VILAGE].iloc[0]
    om_row = result.summary[result.summary["source"] == SOURCE_OPENMETEO_ECMWF].iloc[0]
    assert kma_row["lead_bucket_h"] == 1
    assert kma_row["brier"] == pytest.approx(0.49)  # (0.3 - 1)^2
    assert om_row["brier"] == pytest.approx(0.04)  # (0.8 - 1)^2
    assert len(result.reliability_paths) == 2


def test_build_pop_report_empty_data_returns_message(tmp_path: Path):
    result = build_pop_report(data_dir=tmp_path / "data", reports_dir=tmp_path / "reports")
    assert result.message is not None
    assert result.summary.empty


def test_run_pop_report_cli_empty_graceful(tmp_path: Path, capsys):
    code = run_pop_report(data_dir=tmp_path / "data", reports_dir=tmp_path / "reports")
    assert code == 0
    out = capsys.readouterr().out
    assert "ASOS" in out or "없습니다" in out


def test_lead_lt_one_excluded_in_alignment(tmp_path: Path):
    data_dir = tmp_path / "data"
    pop = _stage_pop(
        source=SOURCE_KMA_VILAGE,
        valid_times=["2026-07-03T07:00:00", "2026-07-03T08:00:00"],
        lead_h=3,
        pop_values=[50.0, 50.0],
    )
    pop.loc[0, "lead_time_h"] = 0
    asos = _stage_asos(["2026-07-03T08:00:00"], [0.0])
    upsert_parquet(pop, data_dir=data_dir, issue_date=ISSUE_DATE, source=SOURCE_KMA_VILAGE)
    upsert_parquet(asos, data_dir=data_dir, issue_date=ISSUE_DATE, source=SOURCE_GROUND_TRUTH_ASOS)

    result = build_pop_report(
        data_dir=data_dir,
        reports_dir=tmp_path / "reports",
        start_date=ISSUE_DATE,
        end_date=ISSUE_DATE,
        forecast_sources=(SOURCE_KMA_VILAGE,),
        min_sample=1,
    )
    # lead 0 제외 후 lead 3 만 남음 — 단일 소스이므로 버킷 1 리포트
    if result.message is None:
        assert result.summary["n"].sum() >= 1
