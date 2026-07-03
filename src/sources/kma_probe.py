"""기상청 단기예보 API — 인증키 검증 + 아카이브 깊이 진단.

순수 진단용. schema/align/metrics 연동 없음.
실행: python -m src.sources.kma_probe
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import requests

from src.sources.kma_auth import (
    ENDPOINTS,
    KST,
    MAX_DAYS_BACK,
    REQUEST_INTERVAL_SEC,
    SEOUL_NX,
    SEOUL_NY,
    KeyAttempt,
    is_auth_failure,
    load_keys,
    parse_api_payload,
    pick_base_time,
    try_all_keys,
    vilage_fcst_params,
)


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


def _print_key_setup_help() -> None:
    from src.sources.kma_auth import ROOT

    env_path = ROOT / ".env"
    example_path = ROOT / ".env.example"
    print("기상청 API 키가 설정되지 않았습니다.", file=sys.stderr)
    print(f"  1) {example_path} 를 참고해 {env_path} 파일을 만드세요.", file=sys.stderr)
    print("  2) KMA_API_KEY_DECODING (원본 키) 또는 DATA_GO_KR_API_KEY", file=sys.stderr)
    print("     KMA_API_KEY_ENCODING (URL 인코딩 키, 선택)", file=sys.stderr)
    print("  3) python -m src.sources.kma_probe 로 다시 실행하세요.", file=sys.stderr)


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
    base_time = pick_base_time(now.date(), now)
    params = vilage_fcst_params(base_date, base_time)

    _, success, attempts, _ = try_all_keys(decoding_key, encoding_key, params, sess)
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
    base_time = pick_base_time(base_day, now)
    params = vilage_fcst_params(base_date, base_time)

    payload, _, attempts, last_payload = try_all_keys(decoding_key, encoding_key, params, sess)
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

    code, msg, items = parse_api_payload(payload)
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
    decoding_key, encoding_key = load_keys()
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
        auth_failures = [
            a
            for a in attempts
            if a.result_msg and is_auth_failure(a.result_code or "", a.result_msg)
        ]
        if auth_failures:
            print(
                "인증 관련 오류가 감지되었습니다. Encoding/Decoding 키 형식을 확인하세요.",
                file=sys.stderr,
            )
        return 2

    print(f"\n✓ 키 검증 성공: {success.label}")
    print(f"  사용 키(마스킹): {success.key_mask}")

    today = datetime.now(KST).date()
    rows: list[ProbeRow] = []
    print(f"\n=== 아카이브 깊이 프로브 (오늘~{MAX_DAYS_BACK}일 전) ===")
    header = (
        f"{'base_date':<10} | {'base_time':<9} | {'상태':<8} | "
        f"{'resultCode':<10} | {'fcstDate 범위':<21} | {'행수':>5}"
    )
    print(header)
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
