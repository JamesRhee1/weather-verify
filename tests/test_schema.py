"""schema 검증 회귀 테스트."""

from __future__ import annotations

import pandas as pd
import pytest
from src.schema import SOURCE_GROUND_TRUTH_ASOS, validate_standard_frame


def _valid_row() -> dict:
    return {
        "station": "seoul",
        "valid_time": pd.Timestamp("2026-06-01T12:00", tz="UTC"),
        "lead_time_h": 0,
        "variable": "temperature_2m",
        "value": 20.0,
        "source": SOURCE_GROUND_TRUTH_ASOS,
    }


def test_validate_standard_frame_accepts_valid_frame():
    df = pd.DataFrame([_valid_row()])
    validate_standard_frame(df, "ok")


def test_validate_standard_frame_rejects_non_datetime_valid_time():
    row = _valid_row()
    row["valid_time"] = "2026-06-01T12:00"
    df = pd.DataFrame([row])
    with pytest.raises(TypeError, match="datetime"):
        validate_standard_frame(df, "bad")
