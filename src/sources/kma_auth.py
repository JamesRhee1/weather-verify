"""기상청 단기예보 API 인증·HTTP 호출 공통 모듈."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

KST = ZoneInfo("Asia/Seoul")
VILAGE_FCST_ENDPOINTS = (
    "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst",
    "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst",
)
ASOS_ENDPOINTS = (
    "https://apis.data.go.kr/1360000/AsosHourlyInfoService/getWthrDataList",
    "http://apis.data.go.kr/1360000/AsosHourlyInfoService/getWthrDataList",
)
# 하위 호환
ENDPOINTS = VILAGE_FCST_ENDPOINTS
BASE_TIMES = ("0200", "0500", "0800", "1100", "1400", "1700", "2000", "2300")
SEOUL_NX = 60
SEOUL_NY = 127
MAX_DAYS_BACK = 10
REQUEST_INTERVAL_SEC = 0.5
TIMEOUT_SEC = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 2.0

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class KeyAttempt:
    label: str
    key_mask: str
    ok: bool
    http_status: int | None
    result_code: str | None
    result_msg: str | None
    detail: str


def mask_key(key: str) -> str:
    if not key:
        return "(empty)"
    visible = key[:4]
    return f"{visible}{'*' * max(0, len(key) - 4)}"


def load_keys() -> tuple[str, str]:
    """프로젝트 .env 에서 키 로드."""
    env_path = ROOT / ".env"
    if env_path.is_file():
        load_dotenv(env_path)

    decoding = os.getenv("KMA_API_KEY_DECODING", "").strip()
    encoding = os.getenv("KMA_API_KEY_ENCODING", "").strip()
    if not decoding:
        decoding = os.getenv("DATA_GO_KR_API_KEY", "").strip()
    return decoding, encoding


def pick_base_time(base_day: date, now: datetime) -> str:
    """base_date 에 맞는 발표 시각 선택."""
    if base_day == now.date():
        candidates = [
            datetime(
                base_day.year, base_day.month, base_day.day, int(t[:2]), int(t[2:]), tzinfo=KST
            )
            for t in BASE_TIMES
        ]
        past = [c for c in candidates if c <= now]
        if past:
            return max(past).strftime("%H%M")
        return "0200"
    return "1100"


def vilage_fcst_params(
    base_date: str,
    base_time: str,
    *,
    nx: int = SEOUL_NX,
    ny: int = SEOUL_NY,
    num_of_rows: int = 1000,
) -> dict[str, str | int]:
    return {
        "pageNo": 1,
        "numOfRows": num_of_rows,
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny,
    }


def parse_api_payload(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    header = payload.get("response", {}).get("header", {})
    code = str(header.get("resultCode", ""))
    msg = str(header.get("resultMsg", ""))
    items = payload.get("response", {}).get("body", {}).get("items", {}).get("item", [])
    if isinstance(items, dict):
        items = [items]
    return code, msg, items


def is_auth_failure(code: str, msg: str) -> bool:
    upper = msg.upper()
    if "SERVICE_KEY" in upper or ("KEY" in upper and "ERROR" in upper):
        return True
    if code in {"30", "31", "32"}:
        return True
    return "NOT_REGISTERED" in upper or "AUTH" in upper


def _sanitize_error(msg: str) -> str:
    if "serviceKey=" in msg:
        return msg.split("serviceKey=")[0].rstrip("?& ") + "serviceKey=***"
    return msg


def http_get_with_retry(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str | int] | None = None,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """429/502 등 일시 오류 시 재시도."""
    last_err: str | None = None
    status = 0
    for attempt in range(MAX_RETRIES):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF_SEC * attempt)
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT_SEC)
            status = resp.status_code
            if resp.status_code in {429, 502, 503, 504}:
                last_err = f"HTTP {resp.status_code} (일시 오류)"
                continue
            resp.raise_for_status()
            return resp.status_code, resp.json(), None
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            status = code
            if code in {429, 502, 503, 504} and attempt < MAX_RETRIES - 1:
                last_err = f"HTTP {code} (일시 오류)"
                continue
            return code, None, _sanitize_error(f"HTTP {code}")
        except requests.RequestException as exc:
            last_err = _sanitize_error(str(exc))
            if attempt < MAX_RETRIES - 1:
                continue
            return 0, None, last_err
        except ValueError as exc:
            return 0, None, f"JSON 파싱 실패: {exc}"
    return status, None, last_err or "알 수 없는 오류"


def call_decoding_params(
    decoding_key: str,
    params: dict[str, str | int],
    session: requests.Session,
    *,
    endpoints: tuple[str, ...] = VILAGE_FCST_ENDPOINTS,
) -> tuple[int, dict[str, Any] | None, str | None]:
    status = 0
    err: str | None = None
    for endpoint in endpoints:
        status, payload, err = http_get_with_retry(
            session,
            endpoint,
            params={**params, "serviceKey": decoding_key},
        )
        if payload is not None:
            return status, payload, None
        if err and "일시 오류" not in err:
            return status, None, err
    return status, None, err


def call_encoding_url(
    encoding_key: str,
    params: dict[str, str | int],
    session: requests.Session,
    *,
    endpoints: tuple[str, ...] = VILAGE_FCST_ENDPOINTS,
) -> tuple[int, dict[str, Any] | None, str | None]:
    query = urlencode(params)
    last_err: str | None = None
    status = 0
    for endpoint in endpoints:
        url = f"{endpoint}?serviceKey={encoding_key}&{query}"
        status, payload, err = http_get_with_retry(session, url)
        if payload is not None:
            return status, payload, None
        last_err = err
    return status, None, last_err


def try_all_keys(
    decoding_key: str,
    encoding_key: str,
    params: dict[str, str | int],
    session: requests.Session,
    *,
    endpoints: tuple[str, ...] = VILAGE_FCST_ENDPOINTS,
) -> tuple[dict[str, Any] | None, KeyAttempt | None, list[KeyAttempt], dict[str, Any] | None]:
    """Decoding → Encoding 순으로 키 전략 시도.

    Returns:
        성공 payload (resultCode 00), 성공 attempt, 시도 내역, 마지막 파싱된 payload
    """
    attempts: list[KeyAttempt] = []
    last_payload: dict[str, Any] | None = None

    if decoding_key:
        status, payload, err = call_decoding_params(
            decoding_key, params, session, endpoints=endpoints
        )
        if payload is not None:
            last_payload = payload
            code, msg, _ = parse_api_payload(payload)
            attempt = KeyAttempt(
                label="Decoding 키 + params (requests 자동 인코딩)",
                key_mask=mask_key(decoding_key),
                ok=code == "00",
                http_status=status,
                result_code=code,
                result_msg=msg,
                detail="정상" if code == "00" else msg,
            )
            attempts.append(attempt)
            if code == "00":
                return payload, attempt, attempts, payload
        else:
            attempts.append(
                KeyAttempt(
                    label="Decoding 키 + params (requests 자동 인코딩)",
                    key_mask=mask_key(decoding_key),
                    ok=False,
                    http_status=status or None,
                    result_code=None,
                    result_msg=None,
                    detail=err or "알 수 없는 오류",
                )
            )

    if encoding_key:
        status, payload, err = call_encoding_url(encoding_key, params, session, endpoints=endpoints)
        if payload is not None:
            last_payload = payload
            code, msg, _ = parse_api_payload(payload)
            attempt = KeyAttempt(
                label="Encoding 키 + URL 직접 부착 (이중 인코딩 방지)",
                key_mask=mask_key(encoding_key),
                ok=code == "00",
                http_status=status,
                result_code=code,
                result_msg=msg,
                detail="정상" if code == "00" else msg,
            )
            attempts.append(attempt)
            if code == "00":
                return payload, attempt, attempts, payload
        else:
            attempts.append(
                KeyAttempt(
                    label="Encoding 키 + URL 직접 부착 (이중 인코딩 방지)",
                    key_mask=mask_key(encoding_key),
                    ok=False,
                    http_status=status or None,
                    result_code=None,
                    result_msg=None,
                    detail=err or "알 수 없는 오류",
                )
            )

    return None, None, attempts, last_payload


def fetch_data_go_kr(
    endpoints: tuple[str, ...],
    params: dict[str, str | int],
    session: requests.Session,
    *,
    decoding_key: str | None = None,
    encoding_key: str | None = None,
) -> dict[str, Any]:
    """공공데이터포털 API 호출. resultCode 00 이 아니면 RuntimeError."""
    dec = decoding_key if decoding_key is not None else load_keys()[0]
    enc = encoding_key if encoding_key is not None else load_keys()[1]
    payload, _, _, last_payload = try_all_keys(dec, enc, params, session, endpoints=endpoints)
    payload = payload or last_payload
    if payload is None:
        raise RuntimeError("공공데이터 API 호출 실패")
    code, msg, _ = parse_api_payload(payload)
    if code != "00":
        raise RuntimeError(f"공공데이터 API 오류 resultCode={code} msg={msg}")
    return payload


def fetch_vilage_fcst(
    params: dict[str, str | int],
    session: requests.Session,
    *,
    decoding_key: str | None = None,
    encoding_key: str | None = None,
) -> dict[str, Any]:
    """단기예보 API 호출."""
    return fetch_data_go_kr(
        VILAGE_FCST_ENDPOINTS,
        params,
        session,
        decoding_key=decoding_key,
        encoding_key=encoding_key,
    )
