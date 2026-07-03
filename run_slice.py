"""첫 수직 슬라이스 엔트리포인트.

서울 · temperature_2m · 최근 2주 · 리드타임 24h/48h 글로벌 모델 MAE 표.
"""
from __future__ import annotations

import sys

from src.core.align import align_forecasts_to_truth
from src.core.metrics import mae_by_lead_time
from src.sources.openmeteo import (
    OpenMeteoFetchError,
    fetch_seoul_temperature_slice,
    make_synthetic_seoul_temperature_slice,
)


def main() -> int:
    mode = "live"
    try:
        raw = fetch_seoul_temperature_slice()
    except (OpenMeteoFetchError, OSError) as exc:
        print(f"[fallback] 라이브 API 실패: {exc}", file=sys.stderr)
        print("[fallback] 합성 데이터로 실행합니다.", file=sys.stderr)
        raw = make_synthetic_seoul_temperature_slice()
        mode = "synthetic"

    aligned = align_forecasts_to_truth([raw])
    mae_table = mae_by_lead_time(aligned)

    print()
    print("=== 기상예보 교차검증 — 첫 슬라이스 ===")
    print(f"데이터 모드 : {mode}")
    print("지점        : 서울 (37.5665, 126.9780)")
    print("변수        : temperature_2m")
    print("기간        : 최근 2주")
    print("정답 프록시 : Open-Meteo previous_day0 (source=ground_truth)")
    print("예보 모델   : Open-Meteo ECMWF (source=openmeteo_ecmwf)")
    print()
    print("리드타임별 글로벌 모델 MAE (°C)")
    print(mae_table.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
