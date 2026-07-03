"""KMA vs 글로벌 POP Brier/BSS/reliability 리포트."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from src.core.report import (
    DEFAULT_FORECAST_SOURCES,
    PopReportResult,
    build_pop_report,
    format_summary_table,
    resolve_report_window,
)
from src.sources.kma_auth import ROOT

REPORTS_DIR = ROOT / "reports"
DATA_DIR = ROOT / "data"


def _parse_date(text: str) -> date:
    return date.fromisoformat(text)


def run_pop_report(
    *,
    data_dir: Path = DATA_DIR,
    reports_dir: Path = REPORTS_DIR,
    start_date: date | None = None,
    end_date: date | None = None,
    truth_mode: str = "window_3h",
) -> int:
    if truth_mode not in ("point", "window_3h"):
        print(f"[error] 알 수 없는 truth-mode: {truth_mode}", file=sys.stderr)
        return 1

    window_start, window_end = resolve_report_window(
        data_dir,
        start_date=start_date,
        end_date=end_date,
        forecast_sources=DEFAULT_FORECAST_SOURCES,
    )

    result: PopReportResult = build_pop_report(
        data_dir=data_dir,
        reports_dir=reports_dir,
        start_date=window_start,
        end_date=window_end,
        truth_mode=truth_mode,  # type: ignore[arg-type]
    )

    print()
    print("=== POP 검증 리포트 (Brier / BSS / reliability) ===")
    if window_start and window_end:
        print(f"기간        : {window_start} ~ {window_end} (issue_date 파티션)")
    print(f"truth-mode  : {truth_mode}")
    print(f"비교 소스   : {', '.join(DEFAULT_FORECAST_SOURCES)}")
    print("리드 버킷   : 6h 구간 (소스 공통 버킷만)")
    print(f"reports/    : {reports_dir}")
    print()

    if result.message:
        print(result.message)
        print()
        return 0

    print("소스×리드버킷별 지표")
    print(format_summary_table(result.summary))
    print()
    if result.reliability_paths:
        print("reliability CSV:")
        for path in result.reliability_paths:
            print(f"  {path}")
    else:
        print("reliability CSV: (표본 부족으로 생성된 파일 없음)")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KMA vs 글로벌 POP Brier/BSS/reliability 리포트")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="parquet 루트 (기본: data/)",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=REPORTS_DIR,
        help="reliability CSV 출력 (기본: reports/)",
    )
    parser.add_argument("--start", type=_parse_date, help="issue_date 시작 (YYYY-MM-DD)")
    parser.add_argument("--end", type=_parse_date, help="issue_date 종료 (YYYY-MM-DD)")
    parser.add_argument(
        "--truth-mode",
        choices=["window_3h", "point"],
        default="window_3h",
        help="ASOS 매칭: window_3h(기본, KMA 3h POP 구간) | point(정각 1시간)",
    )
    args = parser.parse_args(argv)

    return run_pop_report(
        data_dir=args.data_dir,
        reports_dir=args.reports_dir,
        start_date=args.start,
        end_date=args.end,
        truth_mode=args.truth_mode,
    )


if __name__ == "__main__":
    raise SystemExit(main())
