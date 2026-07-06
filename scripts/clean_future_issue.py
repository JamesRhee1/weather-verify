#!/usr/bin/env python3
"""data/ parquet 에서 issue_time > now(UTC) 행 스캔·제거."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from src.sources.kma_auth import ROOT
from src.sources.store import drop_future_issue_rows


def _source_from_path(path: Path) -> str:
    for part in path.parts:
        if part.startswith("source="):
            return part.split("=", 1)[1]
    return "unknown"


def clean_future_issue_rows(
    data_dir: Path,
    *,
    apply: bool,
    now: datetime | None = None,
) -> int:
    """미래 issue_time 행 스캔. ``apply`` 시 parquet 덮어쓰기."""
    parquet_root = data_dir / "parquet"
    if not parquet_root.is_dir():
        return 0

    now_ts = pd.Timestamp((now or datetime.now(timezone.utc)).astimezone(timezone.utc))
    total_removed = 0

    for path in sorted(parquet_root.glob("source=*/issue_date=*/forecasts.parquet")):
        frame = pd.read_parquet(path)
        if frame.empty or "issue_time" not in frame.columns:
            continue

        issue = pd.to_datetime(frame["issue_time"], utc=True)
        future = issue > now_ts
        removed = int(future.sum())
        if removed == 0:
            continue

        source = _source_from_path(path)
        print(f"{source}\t{path}\t{removed}")
        total_removed += removed

        if apply:
            cleaned = drop_future_issue_rows(frame, now=now_ts.to_pydatetime())
            cleaned.to_parquet(path, index=False)

    return total_removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="parquet 에서 issue_time > now(UTC) 행 제거",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=ROOT / "data",
        help="data 루트 (기본: 프로젝트 data/)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="스캔만 (기본)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="실제 삭제",
    )
    args = parser.parse_args(argv)

    apply = args.apply
    mode_label = "apply" if apply else "dry-run"
    print(f"# mode={mode_label}  now={datetime.now(timezone.utc).isoformat()}")

    total = clean_future_issue_rows(args.data_dir, apply=apply)
    print(f"# total_removed={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
