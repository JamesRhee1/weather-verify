"""첫 수직 슬라이스 엔트리포인트.

서울 · temperature_2m · 최근 2주 · 리드타임 24h/48h 글로벌 모델 MAE 표.
"""

from __future__ import annotations

import argparse
import sys

from src.core.align import align_forecasts_to_truth
from src.core.metrics import mae_by_lead_time
from src.schema import SOURCE_GROUND_TRUTH_ASOS, SOURCE_OPENMETEO_ECMWF, SOURCE_OPENMETEO_SELF_PROXY
from src.sources.asos import AsosFetchError, fetch_seoul_asos_slice
from src.sources.kma_auth import load_keys
from src.sources.openmeteo import (
    OpenMeteoFetchError,
    fetch_seoul_ecmwf_forecasts,
    fetch_seoul_openmeteo_proxy_truth,
    make_synthetic_seoul_temperature_slice,
)


def _split_synthetic(raw):
    truth = raw[raw["source"] == SOURCE_OPENMETEO_SELF_PROXY]
    forecasts = raw[raw["source"] == SOURCE_OPENMETEO_ECMWF]
    return truth, forecasts


def run_slice(*, truth_mode: str = "asos") -> int:
    mode = "live"
    effective_truth = truth_mode
    truth_label = ""

    try:
        forecasts = fetch_seoul_ecmwf_forecasts()

        if truth_mode == "asos":
            decoding, encoding = load_keys()
            if not decoding and not encoding:
                print(
                    "[fallback] ASOS: API 키 없음 → openmeteo_self_proxy 정답으로 전환",
                    file=sys.stderr,
                )
                effective_truth = "proxy"
            else:
                try:
                    truth = fetch_seoul_asos_slice()
                    truth_label = "ASOS 실측 (source=ground_truth_asos)"
                except (AsosFetchError, RuntimeError, OSError) as exc:
                    print(
                        f"[fallback] ASOS 실패: {exc} → openmeteo_self_proxy 정답으로 전환",
                        file=sys.stderr,
                    )
                    effective_truth = "proxy"

        if effective_truth == "proxy":
            truth = fetch_seoul_openmeteo_proxy_truth()
            truth_label = (
                "Open-Meteo previous_day0 (source=openmeteo_self_proxy) — "
                "같은 모델의 단기예보라 진짜 정답이 아니며 자기일관성 측정에 불과함"
            )

        aligned = align_forecasts_to_truth(
            [truth, forecasts],
            truth_sources=frozenset(
                {
                    SOURCE_GROUND_TRUTH_ASOS
                    if effective_truth == "asos"
                    else SOURCE_OPENMETEO_SELF_PROXY
                }
            ),
        )

    except (OpenMeteoFetchError, OSError) as exc:
        print(f"[fallback] 라이브 API 실패: {exc}", file=sys.stderr)
        print("[fallback] 합성 데이터로 실행합니다.", file=sys.stderr)
        raw = make_synthetic_seoul_temperature_slice()
        truth, forecasts = _split_synthetic(raw)
        effective_truth = "proxy"
        truth_label = (
            "합성 프록시 (source=openmeteo_self_proxy) — "
            "같은 모델의 단기예보라 진짜 정답이 아니며 자기일관성 측정에 불과함"
        )
        mode = "synthetic"
        aligned = align_forecasts_to_truth(
            [truth, forecasts],
            truth_sources=frozenset({SOURCE_OPENMETEO_SELF_PROXY}),
        )

    mae_table = mae_by_lead_time(aligned)

    print()
    print("=== 기상예보 교차검증 — 첫 슬라이스 ===")
    print(f"데이터 모드 : {mode}")
    print(f"정답 모드   : {effective_truth}")
    print("지점        : 서울 (37.5665, 126.9780)")
    print("변수        : temperature_2m")
    print("기간        : 최근 2주")
    print(f"정답        : {truth_label}")
    print("예보 모델   : Open-Meteo ECMWF (source=openmeteo_ecmwf)")
    print()
    print("리드타임별 글로벌 모델 MAE (°C)")
    print(mae_table.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="기상예보 교차검증 첫 슬라이스")
    parser.add_argument(
        "--truth",
        choices=["asos", "proxy"],
        default="asos",
        help="정답 소스 (기본: asos, 키 없으면 proxy fallback)",
    )
    args = parser.parse_args(argv)
    return run_slice(truth_mode=args.truth)


if __name__ == "__main__":
    raise SystemExit(main())
