"""app.py 스모크 — import 및 수집 현황 스캔."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from app import scan_collection_status
from src.schema import SOURCE_KMA_VILAGE, VARIABLE_POP
from src.sources.store import attach_issue_time, upsert_parquet

ISSUE = datetime(2026, 7, 3, 6, 0, tzinfo=timezone.utc)


def test_app_import_and_scan_collection_status(tmp_path: Path):
    import app

    assert callable(app.scan_collection_status)
    assert callable(app.main)

    empty = scan_collection_status(
        tmp_path / "data",
        now=datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc),
    )
    assert len(empty) == 4
    assert (empty["stale"]).all()

    data_dir = tmp_path / "data"
    row = {
        "station": "seoul",
        "valid_time": pd.Timestamp("2026-07-03T12:00", tz="UTC"),
        "lead_time_h": 3,
        "variable": VARIABLE_POP,
        "value": 40.0,
        "source": SOURCE_KMA_VILAGE,
    }
    frame = attach_issue_time(pd.DataFrame([row]), ISSUE)
    upsert_parquet(frame, data_dir=data_dir, issue_date=date(2026, 7, 3), source=SOURCE_KMA_VILAGE)

    status = scan_collection_status(
        data_dir,
        now=datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc),
    )
    kma = status[status["source"] == SOURCE_KMA_VILAGE].iloc[0]
    assert kma["total_rows"] == 1
    assert not kma["stale"]


def test_format_status_elapsed_nan_as_em_dash():
    from app import _format_status_table

    status = pd.DataFrame(
        [
            {
                "source": "ground_truth_asos",
                "recent_issue_time": pd.NaT,
                "total_rows": 0,
                "issue_date_count": 0,
                "elapsed_hours": float("nan"),
                "stale": True,
            },
            {
                "source": "kma_vilage_fcst",
                "recent_issue_time": pd.Timestamp("2026-07-03T06:00", tz="UTC"),
                "total_rows": 1,
                "issue_date_count": 1,
                "elapsed_hours": None,
                "stale": True,
            },
        ]
    )
    display = _format_status_table(status)
    assert display.iloc[0]["elapsed"] == "—"
    assert display.iloc[1]["elapsed"] == "—"
