"""기상청 단기예보 API — 인증키 검증 + 아카이브 깊이 진단.

순수 진단용. schema/align/metrics 연동 없음.
실행: python -m src.sources.kma_probe
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

KST = ZoneInfo("Asia/Seoul")
ENDPOINTS = (
    "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst",
    "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst",
)
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


@dataclass(frozen=True, slots=True)
class ProbeRow:
    base_date: str
    base_time: str
    status: str
    result_code: str
    result_msg: str
    fcst_min: str
    fcst_max: str
    row_count: int


def _mask_key(key: str) -> str:
    if not key:
        return "(empty)"
    visible = key[:4]
    return f"{visible}{'*' * max(0, len(key) - 4)}"


def _load_keys() -> tuple[str, str]:
    """프로젝트 .env 에서 키 로드."""
    env_path = ROOT / ".env"
    if env_path.is_file():
        load_dotenv(env_path)

    decoding = os.getenv("KMA_API_KEY_DECODING", "").strip()
    encoding = os.getenv("KMA_API_KEY_ENCODING", "").strip()
    if not decoding:
        decoding = os.getenv("DATA_GO_KR_API_KEY", "").strip()
    return decoding, encoding


def _print_key_setup_help() -> None:
    env_path = ROOT / ".env"
    example_path = ROOT / ".env.example"
    print("기상청 API 키가 설정되지 않았습니다.", file=sys.stderr)
    print(f"  1) {example_path} 를 참고해 {env_path} 파일을 만드세요.", file=sys.stderr)
    print("  2) KMA_API_KEY_DECODING (원본 키) 또는 DATA_GO_KR_API_KEY", file=sys.stderr)
    print("     KMA_API_KEY_ENCODING (URL 인코딩 키, 선택)", file=sys.stderr)
    print("  3) python -m src.sources.kma_probe 로 다시 실행하세요.", file=sys.stderr)


def _pick_base_time(base_day: date, now: datetime) -> str:
    """base_date 에 맞는 발표 시각 선택."""
    if base_day == now.date():
        candidates = [
            datetime(base_day.year, base_day.month, base_day.day, int(t[:2]), int(t[2:]), tzinfo=KST)
            for t in BASE_TIMES
        ]
        past = [c for c in candidates if c <= now]
        if past:
            return max(past).strftime("%H%M")
        return "0200"
    return "1100"


def _common_params(base_date: str, base_time: str) -> dict[str, str | int]:
    return {
        "pageNo": 1,
        "numOfRows": 1000,
        "dataType": "JSON",
        "base_date": base_date,
        "base_time": base_time,
        "nx": SEOUL_NX,
        "ny": SEOUL_NY,
    }


def _parse_api_payload(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    header = payload.get("response", {}).get("header", {})
    code = str(header.get("resultCode", ""))
    msg = str(header.get("resultMsg", ""))
    items = payload.get("response", {}).get("body", {}).get("items", {}).get("item", [])
    if isinstance(items, dict):
        items = [items]
    return code, msg, items


def _is_auth_failure(code: str, msg: str) -> bool:
    upper = msg.upper()
    if "SERVICE_KEY" in upper or "KEY" in upper and "ERROR" in upper:
        return True
    if code in {"30", "31", "32"}:
        return True
    return "NOT_REGISTERED" in upper or "AUTH" in upper


def _sanitize_error(msg: str) -> str:
    """오류 메시지에서 serviceKey 값 제거."""
    if "serviceKey=" in msg:
        return msg.split("serviceKey=")[0].rstrip("?& ") + "serviceKey=***"
    return msg


def _http_get_with_retry(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str | int] | None = None,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """429/502 등 일시 오류 시 재시도."""
    last_err: str | None = None
    for attempt in range(MAX_RETRIES):
        if attempt > 0:
            time.sleep(RETRY_BACKOFF_SEC * attempt)
        try:
            resp = session.get(url, params=params, timeout=TIMEOUT_SEC)
            if resp.status_code in {429, 502, 503, 504}:
                last_err = f"HTTP {resp.status_code} (일시 오류)"
                continue
            resp.raise_for_status()
            return resp.status_code, resp.json(), None
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
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
    return 0, None, last_err or "알 수 없는 오류"


def _call_decoding_params(
    decoding_key: str,
    params: dict[str, str | int],
    session: requests.Session,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """Decoding 키를 params에 넣어 requests가 인코딩하도록 호출."""
    for endpoint in ENDPOINTS:
        status, payload, err = _http_get_with_retry(
            session,
            endpoint,
            params={**params, "serviceKey": decoding_key},
        )
        if payload is not None:
            return status, payload, None
        if err and "일시 오류" not in err:
            return status, None, err
    return status, None, err


def _call_encoding_url(
    encoding_key: str,
    params: dict[str, str | int],
    session: requests.Session,
) -> tuple[int, dict[str, Any] | None, str | None]:
    """이미 URL 인코딩된 키를 쿼리스트링에 직접 붙여 호출 (이중 인코딩 방지)."""
    query = urlencode(params)
    last_err: str | None = None
    status = 0
    for endpoint in ENDPOINTS:
        url = f"{endpoint}?serviceKey={encoding_key}&{query}"
        status, payload, err = _http_get_with_retry(session, url)
        if payload is not None:
            return status, payload, None
        last_err = err
    return status, None, last_err


@dataclass(frozen=True, slots=True)
class AuthStrategy:
    label: str
    key_mask: str
    mode: str  # "decoding" | "encoding"
    key: str


def _call_with_strategy(
    strategy: AuthStrategy,
    params: dict[str, str | int],
    session: requests.Session,
) -> tuple[int, dict[str, Any] | None, str | None]:
    if strategy.mode == "decoding":
        return _call_decoding_params(strategy.key, params, session)
    return _call_encoding_url(strategy.key, params, session)


def _try_all_keys(
    decoding_key: str,
    encoding_key: str,
    params: dict[str, str | int],
    session: requests.Session,
) -> tuple[dict[str, Any] | None, KeyAttempt | None, list[KeyAttempt], dict[str, Any] | None]:
    """Decoding → Encoding 순으로 키 전략 시도.

    Returns:
        성공 payload, 성공 attempt, 시도 내역, 마지막 파싱된 payload (resultCode 무관)
    """
    attempts: list[KeyAttempt] = []
    last_payload: dict[str, Any] | None = None

    if decoding_key:
        status, payload, err = _call_decoding_params(decoding_key, params, session)
        if payload is not None:
            last_payload = payload
            code, msg, _ = _parse_api_payload(payload)
            attempt = KeyAttempt(
                label="Decoding 키 + params (requests 자동 인코딩)",
                key_mask=_mask_key(decoding_key),
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
                    key_mask=_mask_key(decoding_key),
                    ok=False,
                    http_status=status or None,
                    result_code=None,
                    result_msg=None,
                    detail=err or "알 수 없는 오류",
                )
            )

    if encoding_key:
        status, payload, err = _call_encoding_url(encoding_key, params, session)
        if payload is not None:
            last_payload = payload
            code, msg, _ = _parse_api_payload(payload)
            attempt = KeyAttempt(
                label="Encoding 키 + URL 직접 부착 (이중 인코딩 방지)",
                key_mask=_mask_key(encoding_key),
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
                    key_mask=_mask_key(encoding_key),
                    ok=False,
                    http_status=status or None,
                    result_code=None,
                    result_msg=None,
                    detail=err or "알 수 없는 오류",
                )
            )

    return None, None, attempts, last_payload


def _summarize_items(items: list[dict[str, Any]]) -> tuple[str, str, int]:
    if not items:
        return "-", "-", 0
    dates = [str(row.get("fcstDate", "")) for row in items if row.get("fcstDate")]
    if not dates:
        return "-", "-", len(items)
    return min(dates), max(dates), len(items)


def verify_api_key(
    decoding_key: str,
    encoding_key: str,
    session: requests.Session | None = None,
) -> tuple[KeyAttempt | None, list[KeyAttempt]]:
    """오늘 기준 1회 호출로 키 검증."""
    sess = session or requests.Session()
    now = datetime.now(KST)
    base_date = now.strftime("%Y%m%d")
    base_time = _pick_base_time(now.date(), now)
    params = _common_params(base_date, base_time)

    _, success, attempts, _ = _try_all_keys(decoding_key, encoding_key, params, sess)
    return success, attempts


def probe_base_date(
    base_day: date,
    decoding_key: str,
    encoding_key: str,
    *,
    now: datetime | None = None,
    session: requests.Session | None = None,
) -> ProbeRow:
    """단일 base_date 아카이브 깊이 프로브."""
    sess = session or requests.Session()
    now = now or datetime.now(KST)
    base_date = base_day.strftime("%Y%m%d")
    base_time = _pick_base_time(base_day, now)
    params = _common_params(base_date, base_time)

    payload, _, attempts, last_payload = _try_all_keys(decoding_key, encoding_key, params, sess)
    payload = payload or last_payload
    if payload is None:
        last = attempts[-1] if attempts else None
        return ProbeRow(
            base_date=base_date,
            base_time=base_time,
            status="HTTP/통신 오류",
            result_code=last.result_code or "-",
            result_msg=last.result_msg or (last.detail if last else "호출 실패"),
            fcst_min="-",
            fcst_max="-",
            row_count=0,
        )

    code, msg, items = _parse_api_payload(payload)
    fcst_min, fcst_max, count = _summarize_items(items)
    status = "정상" if code == "00" else "API 오류"
    return ProbeRow(
        base_date=base_date,
        base_time=base_time,
        status=status,
        result_code=code,
        result_msg=msg,
        fcst_min=fcst_min,
        fcst_max=fcst_max,
        row_count=count,
    )


def _print_attempts(attempts: list[KeyAttempt]) -> None:
    print("\n=== 키 인증 시도 내역 ===")
    for i, att in enumerate(attempts, 1):
        mark = "성공" if att.ok else "실패"
        print(f"  [{i}] {att.label}")
        print(f"      키(마스킹): {att.key_mask}  → {mark}")
        if att.http_status is not None:
            print(f"      HTTP: {att.http_status}")
        if att.result_code is not None:
            print(f"      resultCode: {att.result_code}  resultMsg: {att.result_msg}")
        if not att.ok:
            print(f"      사유: {att.detail}")


def _verdict(rows: list[ProbeRow]) -> str:
    today = datetime.now(KST).date()
    max_ok_offset = -1
    for offset in range(MAX_DAYS_BACK + 1):
        target = (today - timedelta(days=offset)).strftime("%Y%m%d")
        row = next((r for r in rows if r.base_date == target), None)
        if row and row.result_code == "00":
            max_ok_offset = offset
        else:
            break
    n = max(0, max_ok_offset)
    label = "백테스트 가능" if n >= 7 else "cron 적재 필요"
    return f"→ 과거 {n}일치까지 조회 가능. [{label}] 판정"


def run_diagnostic() -> int:
    decoding_key, encoding_key = _load_keys()
    if not decoding_key and not encoding_key:
        _print_key_setup_help()
        return 1

    print("=== 기상청 단기예보 API — 키 검증 + 아카이브 깊이 진단 ===")
    print(f"지점: 서울 (nx={SEOUL_NX}, ny={SEOUL_NY})")
    print(f"엔드포인트: {ENDPOINTS[0]}")

    session = requests.Session()
    success, attempts = verify_api_key(decoding_key, encoding_key, session)
    _print_attempts(attempts)

    if success is None:
        print("\n키 인증 실패 — 아카이브 진단을 건너뜁니다.", file=sys.stderr)
        auth_failures = [a for a in attempts if a.result_msg and _is_auth_failure(a.result_code or "", a.result_msg)]
        if auth_failures:
            print("인증 관련 오류가 감지되었습니다. Encoding/Decoding 키 형식을 확인하세요.", file=sys.stderr)
        return 2

    print(f"\n✓ 키 검증 성공: {success.label}")
    print(f"  사용 키(마스킹): {success.key_mask}")

    today = datetime.now(KST).date()
    rows: list[ProbeRow] = []
    print(f"\n=== 아카이브 깊이 프로브 (오늘~{MAX_DAYS_BACK}일 전) ===")
    print(f"{'base_date':<10} | {'base_time':<9} | {'상태':<8} | {'resultCode':<10} | {'fcstDate 범위':<21} | {'행수':>5}")
    print("-" * 80)

    for offset in range(MAX_DAYS_BACK + 1):
        if offset > 0:
            time.sleep(REQUEST_INTERVAL_SEC)
        base_day = today - timedelta(days=offset)
        row = probe_base_date(base_day, decoding_key, encoding_key, session=session)
        rows.append(row)
        fcst_range = f"{row.fcst_min}~{row.fcst_max}" if row.fcst_min != "-" else "-"
        msg_short = (row.result_msg[:18] + "…") if len(row.result_msg) > 19 else row.result_msg
        print(
            f"{row.base_date:<10} | {row.base_time:<9} | {row.status:<8} | "
            f"{row.result_code:<10} | {fcst_range:<21} | {row.row_count:>5}"
        )
        if row.status != "정상" and msg_short:
            print(f"           └ resultMsg: {msg_short}")

    print()
    print(_verdict(rows))
    return 0


def main() -> None:
    raise SystemExit(run_diagnostic())


if __name__ == "__main__":
    main()
