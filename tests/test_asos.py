"""ASOS 파싱 단위 테스트 (네트워크 없음)."""

from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

import pandas as pd
import pytest
from src.schema import SOURCE_GROUND_TRUTH_ASOS, VARIABLE_PCP, VARIABLE_TEMPERATURE
from src.sources.asos import (
    parse_asos_items_to_long,
    parse_asos_payload,
    parse_observation_time,
    parse_rn_observation,
)

FIXTURE = Path(__file__).parent / "fixtures" / "asos_hourly_sample.json"


@pytest.fixture
def sample_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parse_observation_time_kst_to_utc():
    utc = parse_observation_time("2026-06-01 12:00")
    assert utc == pd.Timestamp("2026-06-01 03:00", tz="UTC").to_pydatetime().replace(
        tzinfo=timezone.utc
    )


def test_parse_asos_payload(sample_payload):
    frame = parse_asos_payload(sample_payload)
    assert (frame["source"] == SOURCE_GROUND_TRUTH_ASOS).all()
    assert (frame["lead_time_h"] == 0).all()

    temps = frame[frame["variable"] == VARIABLE_TEMPERATURE]
    assert len(temps) == 2
    assert temps.iloc[0]["value"] == pytest.approx(22.3)

    pcp = frame[frame["variable"] == VARIABLE_PCP]
    assert len(pcp) == 3
    assert pcp.iloc[1]["value"] == pytest.approx(1.2)


def test_parse_asos_items_skips_missing_temperature():
    items = [{"tm": "2026-06-01 14:00", "ta": "", "rn": "0.0", "rnQcflag": "0"}]
    frame = parse_asos_items_to_long(items)
    assert frame["variable"].tolist() == [VARIABLE_PCP]
    assert frame.iloc[0]["value"] == 0.0


def test_parse_rn_qcflag_zero_empty_rn_is_no_rain():
    item = {"rn": "", "rnQcflag": "0"}
    assert parse_rn_observation(item) == 0.0


def test_parse_rn_qcflag_missing_skips_row():
    item = {"rn": "", "rnQcflag": "9"}
    assert parse_rn_observation(item) is None
    frame = parse_asos_items_to_long([{"tm": "2026-06-01 15:00", **item}])
    assert frame.empty


def test_parse_rn_without_qcflag_blank_rn_skips_row():
    item = {"rn": ""}
    assert parse_rn_observation(item) is None
    frame = parse_asos_items_to_long([{"tm": "2026-06-01 16:00", "ta": "20.0", "rn": ""}])
    assert frame["variable"].tolist() == [VARIABLE_TEMPERATURE]


def test_parse_rn_without_qcflag_numeric_rn_ok():
    assert parse_rn_observation({"rn": "2.5"}) == pytest.approx(2.5)
