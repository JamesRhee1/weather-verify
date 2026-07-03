"""기상청 ASOS 시간자료 API → 표준 long-format (실측 정답)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests

from src.schema import (
    SEOUL_ASOS_STN_ID,
    SOURCE_GROUND_TRUTH_ASOS,
    STANDARD_COLUMNS,
    STATION_SEOUL,
    VARIABLE_PCP,
    VARIABLE_TEMPERATURE,
    validate_standard_frame,
)
from src.sources.kma_auth import (
    ASOS_ENDPOINTS,
    KST,
    fetch_data_go_kr,
    load_keys,
    parse_api_payload,
)

PAST_DAYS = 14
_PAGE_SIZE = 1000

_ASOS_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("ta", VARIABLE_TEMPERATURE),
    ("rn", VARIABLE_PCP),
)


class AsosFetchError(RuntimeError):
    """ASOS API 호출·파싱 실패."""


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(STANDARD_COLUMNS))


def asos_hourly_params(
    start_kst: datetime,
    end_kst: datetime,
    *,
    stn_id: int = SEOUL_ASOS_STN_ID,
    page_no: int = 1,
    num_of_rows: int = _PAGE_SIZE,
) -> dict[str, str | int]:
    return {
        "pageNo": page_no,
        "numOfRows": num_of_rows,
        "dataType": "JSON",
        "dataCd": "ASOS",
        "dateCd": "HR",
        "startDt": start_kst.strftime("%Y%m%d"),
        "startHh": start_kst.strftime("%H"),
        "endDt": end_kst.strftime("%Y%m%d"),
        "endHh": end_kst.strftime("%H"),
        "stnIds": stn_id,
    }


def parse_observation_time(tm: str) -> datetime:
    """ASOS tm (KST) → UTC."""
    obs_kst = pd.to_datetime(tm).tz_localize(KST)
    return obs_kst.to_pydatetime().astimezone(timezone.utc)


def parse_optional_float(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_asos_items_to_long(
    items: list[dict[str, Any]],
    *,
    station: str = STATION_SEOUL,
) -> pd.DataFrame:
    """ASOS item 목록 → 표준 long-format (기온·강수량, lead_time_h=0)."""
    rows: list[dict[str, Any]] = []
    for item in items:
        tm = item.get("tm")
        if not tm:
            continue
        valid_time = parse_observation_time(str(tm))

        for api_field, variable in _ASOS_FIELD_MAP:
            value = parse_optional_float(item.get(api_field))
            if value is None:
                if api_field == "rn":
                    value = 0.0
                else:
                    continue
            rows.append(
                {
                    "station": station,
                    "valid_time": valid_time,
                    "lead_time_h": 0,
                    "variable": variable,
                    "value": value,
                    "source": SOURCE_GROUND_TRUTH_ASOS,
                }
            )

    if not rows:
        return _empty_frame()

    frame = pd.DataFrame(rows, columns=list(STANDARD_COLUMNS))
    validate_standard_frame(frame, "parse_asos_items_to_long")
    return frame


def parse_asos_payload(
    payload: dict[str, Any],
    *,
    station: str = STATION_SEOUL,
) -> pd.DataFrame:
    code, msg, items = parse_api_payload(payload)
    if code != "00":
        raise ValueError(f"resultCode={code} msg={msg}")
    return parse_asos_items_to_long(items, station=station)


def _default_time_window_kst(
    *,
    past_days: int,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """ASOS 조회 구간 (KST). API는 전일(D-1)까지 제공."""
    now_kst = (now or datetime.now(KST)).astimezone(KST)
    end_kst = (now_kst - timedelta(days=1)).replace(minute=0, second=0, microsecond=0, hour=23)
    start_kst = (end_kst - timedelta(days=past_days - 1)).replace(hour=0)
    return start_kst, end_kst


def fetch_asos_hourly_all_pages(
    start_kst: datetime,
    end_kst: datetime,
    session: requests.Session,
    *,
    stn_id: int = SEOUL_ASOS_STN_ID,
    decoding_key: str | None = None,
    encoding_key: str | None = None,
) -> list[dict[str, Any]]:
    """페이지네이션 처리해 전체 item 반환."""
    page = 1
    all_items: list[dict[str, Any]] = []
    total_count: int | None = None

    while True:
        params = asos_hourly_params(start_kst, end_kst, stn_id=stn_id, page_no=page)
        payload = fetch_data_go_kr(
            ASOS_ENDPOINTS,
            params,
            session,
            decoding_key=decoding_key,
            encoding_key=encoding_key,
        )
        code, msg, items = parse_api_payload(payload)
        if code != "00":
            raise AsosFetchError(f"resultCode={code} msg={msg}")

        body = payload.get("response", {}).get("body", {})
        if total_count is None:
            total_count = int(body.get("totalCount", len(items)))

        all_items.extend(items)
        if len(all_items) >= total_count or not items:
            break
        page += 1

    return all_items


def fetch_seoul_asos_slice(
    *,
    past_days: int = PAST_DAYS,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """서울 ASOS 시간자료 — 기온·강수량 실측."""
    decoding, encoding = load_keys()
    if not decoding and not encoding:
        raise AsosFetchError("공공데이터 API 키가 설정되지 않았습니다.")

    start_kst, end_kst = _default_time_window_kst(past_days=past_days, now=now)
    sess = session or requests.Session()
    items = fetch_asos_hourly_all_pages(
        start_kst,
        end_kst,
        sess,
        decoding_key=decoding,
        encoding_key=encoding,
    )
    frame = parse_asos_items_to_long(items)
    if frame.empty:
        raise AsosFetchError("유효한 ASOS 관측 행이 없습니다.")
    return frame
