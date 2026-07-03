"""align 모듈 합성 데이터 테스트 (네트워크 없음)."""

from __future__ import annotations

import pandas as pd
import pytest
from src.core.align import align_forecasts_to_truth
from src.schema import SOURCE_GROUND_TRUTH_ASOS, SOURCE_OPENMETEO_ECMWF, SOURCE_OPENMETEO_SELF_PROXY


def _row(
    valid_time: str,
    lead: int,
    value: float,
    source: str,
) -> dict:
    return {
        "station": "seoul",
        "valid_time": pd.Timestamp(valid_time, tz="UTC"),
        "lead_time_h": lead,
        "variable": "temperature_2m",
        "value": value,
        "source": source,
    }


def test_align_joins_forecast_with_ground_truth():
    truth = pd.DataFrame(
        [
            _row("2026-06-01T12:00", 0, 20.0, SOURCE_GROUND_TRUTH_ASOS),
            _row("2026-06-01T13:00", 0, 21.0, SOURCE_GROUND_TRUTH_ASOS),
        ]
    )
    fcst = pd.DataFrame(
        [
            _row("2026-06-01T12:00", 24, 21.0, SOURCE_OPENMETEO_ECMWF),
            _row("2026-06-01T13:00", 24, 22.0, SOURCE_OPENMETEO_ECMWF),
        ]
    )
    aligned = align_forecasts_to_truth([truth, fcst])

    assert len(aligned) == 2
    assert set(aligned["lead_time_h"]) == {24}
    row = aligned[aligned["valid_time"] == pd.Timestamp("2026-06-01T12:00", tz="UTC")].iloc[0]
    assert row["forecast_value"] == 21.0
    assert row["truth_value"] == 20.0


def test_align_skips_mismatched_valid_time():
    truth = pd.DataFrame([_row("2026-06-01T12:00", 0, 20.0, SOURCE_GROUND_TRUTH_ASOS)])
    fcst = pd.DataFrame([_row("2026-06-01T15:00", 24, 21.0, SOURCE_OPENMETEO_ECMWF)])
    aligned = align_forecasts_to_truth([truth, fcst])
    assert aligned.empty


def test_align_drops_rows_with_missing_values():
    truth = pd.DataFrame(
        [
            _row("2026-06-01T12:00", 0, 20.0, SOURCE_GROUND_TRUTH_ASOS),
            _row("2026-06-01T13:00", 0, 20.0, SOURCE_GROUND_TRUTH_ASOS),
        ]
    )
    fcst = pd.DataFrame(
        [
            _row("2026-06-01T12:00", 24, float("nan"), SOURCE_OPENMETEO_ECMWF),
            _row("2026-06-01T13:00", 48, 19.0, SOURCE_OPENMETEO_ECMWF),
        ]
    )
    aligned = align_forecasts_to_truth([truth, fcst])
    assert len(aligned) == 1
    assert aligned.iloc[0]["lead_time_h"] == 48


def test_align_rejects_non_datetime_valid_time():
    bad = pd.DataFrame(
        [
            {
                "station": "seoul",
                "valid_time": "2026-06-01T12:00",
                "lead_time_h": 0,
                "variable": "temperature_2m",
                "value": 20.0,
                "source": SOURCE_GROUND_TRUTH_ASOS,
            }
        ]
    )
    with pytest.raises(TypeError, match="datetime"):
        align_forecasts_to_truth([bad])


def test_align_rejects_naive_valid_time():
    bad = pd.DataFrame(
        [
            {
                "station": "seoul",
                "valid_time": pd.Timestamp("2026-06-01T12:00"),
                "lead_time_h": 0,
                "variable": "temperature_2m",
                "value": 20.0,
                "source": SOURCE_GROUND_TRUTH_ASOS,
            }
        ]
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        align_forecasts_to_truth([bad])


def test_align_accepts_openmeteo_self_proxy():
    truth = pd.DataFrame([_row("2026-06-01T12:00", 0, 20.0, SOURCE_OPENMETEO_SELF_PROXY)])
    fcst = pd.DataFrame([_row("2026-06-01T12:00", 24, 21.0, SOURCE_OPENMETEO_ECMWF)])
    aligned = align_forecasts_to_truth(
        [truth, fcst],
        truth_sources=frozenset({SOURCE_OPENMETEO_SELF_PROXY}),
    )
    assert len(aligned) == 1


def test_align_rejects_missing_standard_columns():
    bad = pd.DataFrame({"station": ["seoul"]})
    with pytest.raises(ValueError, match="표준 스키마"):
        align_forecasts_to_truth([bad])
