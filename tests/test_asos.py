"""ASOS 파싱 단위 테스트 (네트워크 없음)."""

from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path

import pandas as pd
import pytest
from src.schema import SOURCE_GROUND_TRUTH_ASOS, VARIABLE_PCP, VARIABLE_TEMPERATURE
from src.sources.asos import (
    _PAGE_SIZE,
    asos_hourly_params,
    parse_asos_items_to_long,
    parse_asos_payload,
    parse_observation_time,
    parse_rn_observation,
)

FIXTURE = Path(__file__).parent / "fixtures" / "asos_hourly_sample.json"


def test_asos_page_size_below_api_limit():
    assert _PAGE_SIZE < 1000


def test_asos_hourly_params_stn_ids_is_string():
    start = pd.Timestamp("2026-07-05", tz="Asia/Seoul").to_pydatetime()
    end = pd.Timestamp("2026-07-05 23:00", tz="Asia/Seoul").to_pydatetime()
    params = asos_hourly_params(start, end)
    assert params["numOfRows"] < 1000
    assert params["stnIds"] == "108"


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
