"""POP 검증 리포트 — parquet 로드·정렬·Brier/BSS/reliability (순수 pandas)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta, timezone
from pathlib import Path
from typing import Literal

import pandas as pd

from src.core.metrics import brier_score, brier_skill_score, reliability_table
from src.core.precip import PRECIP_BINARY_THRESHOLD_MM, precip_to_binary
from src.schema import (
    SOURCE_GROUND_TRUTH_ASOS,
    SOURCE_KMA_VILAGE,
    SOURCE_OPENMETEO_ECMWF,
    SOURCE_OPENMETEO_GFS,
    STATION_SEOUL,
    VARIABLE_PCP,
    VARIABLE_POP,
)
from src.sources.store import parquet_partition_path

TruthMatchMode = Literal["point", "window_3h"]

DEFAULT_FORECAST_SOURCES: tuple[str, ...] = (
    SOURCE_KMA_VILAGE,
    SOURCE_OPENMETEO_ECMWF,
    SOURCE_OPENMETEO_GFS,
)
LEAD_BUCKET_HOURS = 6
MIN_SAMPLE_SIZE = 30
KMA_WINDOW_HOURS = 3


@dataclass(frozen=True)
class PopBucketMetrics:
    source: str
    lead_bucket_h: int
    brier: float | None
    bss: float | None
    n: int
    sample_insufficient: bool
    reliability: pd.DataFrame


@dataclass(frozen=True)
class PopReportResult:
    """POP 리포트 결과. ``summary`` 가 비어 있으면 ``message`` 로 안내."""

    summary: pd.DataFrame
    bucket_metrics: tuple[PopBucketMetrics, ...]
    reliability_paths: tuple[Path, ...]
    message: str | None = None


def lead_time_bucket_lower(lead_time_h: int, *, bucket_hours: int = LEAD_BUCKET_HOURS) -> int:
    """리드타임을 6h 구간 하한(1, 7, 13, …)으로 매핑. ``lead_time_h < 1`` 이면 -1."""
    if lead_time_h < 1:
        return -1
    return ((lead_time_h - 1) // bucket_hours) * bucket_hours + 1


def lead_bucket_label(lead_bucket_h: int, *, bucket_hours: int = LEAD_BUCKET_HOURS) -> str:
    upper = lead_bucket_h + bucket_hours - 1
    return f"{lead_bucket_h}-{upper}h"


def pop_percent_to_prob(values: pd.Series) -> pd.Series:
    """저장된 POP(0~100 %) → Brier용 확률(0~1)."""
    return values.astype(float) / 100.0


def discover_issue_dates(data_dir: Path, source: str) -> list[date]:
    root = data_dir / "parquet" / f"source={source}"
    if not root.is_dir():
        return []
    dates: list[date] = []
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.startswith("issue_date="):
            dates.append(date.fromisoformat(entry.name.split("=", 1)[1]))
    return sorted(dates)


def load_parquet_for_source(
    data_dir: Path,
    source: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    variable: str | None = None,
    station: str = STATION_SEOUL,
) -> pd.DataFrame:
    """source 파티션 parquet 를 기간·변수로 필터해 로드."""
    issue_dates = discover_issue_dates(data_dir, source)
    if start_date is not None:
        issue_dates = [d for d in issue_dates if d >= start_date]
    if end_date is not None:
        issue_dates = [d for d in issue_dates if d <= end_date]
    if not issue_dates:
        return pd.DataFrame()

    frames: list[pd.DataFrame] = []
    for issue_date in issue_dates:
        path = parquet_partition_path(data_dir, issue_date, source)
        if not path.is_file():
            continue
        part = pd.read_parquet(path)
        if part.empty:
            continue
        if "station" in part.columns:
            part = part[part["station"] == station]
        if variable is not None:
            part = part[part["variable"] == variable]
        if not part.empty:
            frames.append(part)

    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    out["valid_time"] = pd.to_datetime(out["valid_time"], utc=True)
    if "issue_time" in out.columns:
        out["issue_time"] = pd.to_datetime(out["issue_time"], utc=True)
    dedupe_keys = ("source", "issue_time", "station", "valid_time", "variable")
    dedupe_cols = [c for c in dedupe_keys if c in out.columns]
    return out.drop_duplicates(subset=dedupe_cols, keep="last")


def _asos_hourly_pcp(asos: pd.DataFrame) -> pd.DataFrame:
    """ASOS 시간당 강수량(mm) — valid_time 유일."""
    if asos.empty:
        return pd.DataFrame(columns=["valid_time", "pcp_mm"])
    pcp = asos[asos["variable"] == VARIABLE_PCP].copy()
    if pcp.empty:
        return pd.DataFrame(columns=["valid_time", "pcp_mm"])
    keyed = (
        pcp.groupby("valid_time", as_index=False)["value"]
        .first()
        .rename(columns={"value": "pcp_mm"})
        .sort_values("valid_time")
    )
    return keyed


def truth_outcome_point(
    valid_time: pd.Timestamp,
    asos_pcp: pd.DataFrame,
    *,
    threshold: float = PRECIP_BINARY_THRESHOLD_MM,
) -> int | None:
    """해당 ``valid_time`` 한 시각의 ASOS 강수 이진값.

    KMA 3h POP은 구간 강수를 요약한 값이라 **정각 1시간만** 보면 제품 정의와
    어긋날 수 있다. 엄밀 시각 대조·민감도 분석용.
    """
    row = asos_pcp[asos_pcp["valid_time"] == valid_time]
    if row.empty:
        return None
    return precip_to_binary(float(row.iloc[0]["pcp_mm"]), threshold=threshold)


def truth_outcome_window_3h(
    valid_time: pd.Timestamp,
    asos_pcp: pd.DataFrame,
    *,
    window_hours: int = KMA_WINDOW_HOURS,
    threshold: float = PRECIP_BINARY_THRESHOLD_MM,
) -> int | None:
    """``(valid_time - window, valid_time]`` 구간 ASOS 강수 유무 (기본 3h).

    KMA 단기 POP은 3시간 간격 예보이며, 통상 해당 유효시각까지의 구간 강수를
    반영한다. **기본값**으로 이 구간 내 1시간이라도 ≥ threshold mm 이면 1.
    구간 내 관측이 없으면 None (행 제외).
    """
    window_start = valid_time - timedelta(hours=window_hours)
    mask = (asos_pcp["valid_time"] > window_start) & (asos_pcp["valid_time"] <= valid_time)
    subset = asos_pcp.loc[mask, "pcp_mm"]
    if subset.empty:
        return None
    return 1 if (subset >= threshold).any() else 0


def _resolve_truth_outcome(
    valid_time: pd.Timestamp,
    asos_pcp: pd.DataFrame,
    *,
    truth_mode: TruthMatchMode,
) -> int | None:
    if truth_mode == "point":
        return truth_outcome_point(valid_time, asos_pcp)
    return truth_outcome_window_3h(valid_time, asos_pcp)


def align_pop_forecasts_to_truth(
    pop_forecasts: pd.DataFrame,
    asos: pd.DataFrame,
    *,
    truth_mode: TruthMatchMode = "window_3h",
    bucket_hours: int = LEAD_BUCKET_HOURS,
) -> pd.DataFrame:
    """POP 예보를 ASOS 강수 이진 실측과 정렬.

    Returns:
        station, valid_time, lead_time_h, lead_bucket_h, source, forecast_pct, prob, outcome
    """
    if pop_forecasts.empty:
        return pd.DataFrame(
            columns=[
                "station",
                "valid_time",
                "lead_time_h",
                "lead_bucket_h",
                "source",
                "forecast_pct",
                "prob",
                "outcome",
            ]
        )

    asos_pcp = _asos_hourly_pcp(asos)
    rows: list[dict[str, object]] = []

    for _, row in pop_forecasts.iterrows():
        valid_time = pd.Timestamp(row["valid_time"]).tz_convert(timezone.utc)
        lead_h = int(row["lead_time_h"])
        bucket = lead_time_bucket_lower(lead_h, bucket_hours=bucket_hours)
        if bucket < 0:
            continue

        outcome = _resolve_truth_outcome(valid_time, asos_pcp, truth_mode=truth_mode)
        if outcome is None:
            continue

        forecast_pct = float(row["value"])
        rows.append(
            {
                "station": row["station"],
                "valid_time": valid_time,
                "lead_time_h": lead_h,
                "lead_bucket_h": bucket,
                "source": row["source"],
                "forecast_pct": forecast_pct,
                "prob": forecast_pct / 100.0,
                "outcome": int(outcome),
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                "station",
                "valid_time",
                "lead_time_h",
                "lead_bucket_h",
                "source",
                "forecast_pct",
                "prob",
                "outcome",
            ]
        )

    return pd.DataFrame(rows)


def common_lead_buckets(aligned: pd.DataFrame, sources: tuple[str, ...]) -> set[int]:
    """모든 소스에 데이터가 있는 리드 버킷 교집합."""
    if aligned.empty or not sources:
        return set()

    per_source: list[set[int]] = []
    for source in sources:
        buckets = set(aligned.loc[aligned["source"] == source, "lead_bucket_h"].unique())
        if not buckets:
            return set()
        per_source.append(buckets)
    common = per_source[0].copy()
    for other in per_source[1:]:
        common &= other
    return common


def compute_bucket_metrics(
    aligned: pd.DataFrame,
    *,
    source: str,
    lead_bucket_h: int,
    min_sample: int = MIN_SAMPLE_SIZE,
) -> PopBucketMetrics:
    subset = aligned[(aligned["source"] == source) & (aligned["lead_bucket_h"] == lead_bucket_h)]
    pair = subset[["prob", "outcome"]].dropna()
    n = len(pair)
    insufficient = n < min_sample

    if insufficient or pair.empty:
        return PopBucketMetrics(
            source=source,
            lead_bucket_h=lead_bucket_h,
            brier=None,
            bss=None,
            n=n,
            sample_insufficient=True,
            reliability=pd.DataFrame(),
        )

    metric_df = pair.rename(columns={"prob": "prob", "outcome": "outcome"})
    bs = brier_score(metric_df, "prob", "outcome")
    bss = brier_skill_score(metric_df, "prob", "outcome")
    rel = reliability_table(metric_df, "prob", "outcome")

    return PopBucketMetrics(
        source=source,
        lead_bucket_h=lead_bucket_h,
        brier=bs,
        bss=bss,
        n=n,
        sample_insufficient=False,
        reliability=rel,
    )


def metrics_to_summary_row(metrics: PopBucketMetrics) -> dict[str, object]:
    label = lead_bucket_label(metrics.lead_bucket_h)
    if metrics.sample_insufficient:
        return {
            "source": metrics.source,
            "lead_bucket": label,
            "lead_bucket_h": metrics.lead_bucket_h,
            "brier": "표본 부족",
            "bss": "표본 부족",
            "n": metrics.n,
        }
    return {
        "source": metrics.source,
        "lead_bucket": label,
        "lead_bucket_h": metrics.lead_bucket_h,
        "brier": metrics.brier,
        "bss": metrics.bss,
        "n": metrics.n,
    }


def write_reliability_csv(
    metrics: PopBucketMetrics,
    reports_dir: Path,
) -> Path | None:
    if metrics.sample_insufficient or metrics.reliability.empty:
        return None
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"reliability_{metrics.source}_lead{metrics.lead_bucket_h}h.csv"
    out = metrics.reliability.copy()
    out.insert(0, "source", metrics.source)
    out.insert(1, "lead_bucket_h", metrics.lead_bucket_h)
    out.to_csv(path, index=False)
    return path


def infer_date_range(
    data_dir: Path,
    sources: tuple[str, ...],
) -> tuple[date | None, date | None]:
    all_dates: list[date] = []
    for source in sources:
        all_dates.extend(discover_issue_dates(data_dir, source))
    if not all_dates:
        return None, None
    return min(all_dates), max(all_dates)


def build_pop_report(
    *,
    data_dir: Path,
    reports_dir: Path,
    start_date: date | None = None,
    end_date: date | None = None,
    truth_mode: TruthMatchMode = "window_3h",
    forecast_sources: tuple[str, ...] = DEFAULT_FORECAST_SOURCES,
    min_sample: int = MIN_SAMPLE_SIZE,
    lead_bucket_hours: int = LEAD_BUCKET_HOURS,
    station: str = STATION_SEOUL,
) -> PopReportResult:
    """적재 parquet → POP vs ASOS Brier/BSS/reliability 리포트."""
    empty_cols = ["source", "lead_bucket", "lead_bucket_h", "brier", "bss", "n"]

    asos = load_parquet_for_source(
        data_dir,
        SOURCE_GROUND_TRUTH_ASOS,
        start_date=start_date,
        end_date=end_date,
        station=station,
    )
    if asos.empty:
        return PopReportResult(
            summary=pd.DataFrame(columns=empty_cols),
            bucket_metrics=(),
            reliability_paths=(),
            message=(
                "ASOS 실측 parquet 가 없습니다. "
                "`python -m src.sources.asos --collect` 로 적재하세요."
            ),
        )

    pop_frames: list[pd.DataFrame] = []
    loaded_sources: list[str] = []
    for source in forecast_sources:
        part = load_parquet_for_source(
            data_dir,
            source,
            start_date=start_date,
            end_date=end_date,
            variable=VARIABLE_POP,
            station=station,
        )
        if not part.empty:
            pop_frames.append(part)
            loaded_sources.append(source)

    if not pop_frames:
        return PopReportResult(
            summary=pd.DataFrame(columns=empty_cols),
            bucket_metrics=(),
            reliability_paths=(),
            message=(
                "POP 예보 parquet 가 없습니다. "
                "KMA: `python -m src.sources.kma --collect`, "
                "글로벌: `python -m src.sources.openmeteo --collect`"
            ),
        )

    pop_forecasts = pd.concat(pop_frames, ignore_index=True)
    aligned = align_pop_forecasts_to_truth(
        pop_forecasts,
        asos,
        truth_mode=truth_mode,
        bucket_hours=lead_bucket_hours,
    )
    if aligned.empty:
        return PopReportResult(
            summary=pd.DataFrame(columns=empty_cols),
            bucket_metrics=(),
            reliability_paths=(),
            message=(
                "POP 예보와 ASOS 실측을 매칭한 행이 없습니다. "
                "기간·적재 데이터를 확인하거나 truth-mode 를 바꿔 보세요."
            ),
        )

    compare_sources = tuple(s for s in forecast_sources if s in loaded_sources)
    buckets = common_lead_buckets(aligned, compare_sources)
    if not buckets:
        return PopReportResult(
            summary=pd.DataFrame(columns=empty_cols),
            bucket_metrics=(),
            reliability_paths=(),
            message="소스 공통 리드 버킷이 없습니다. 각 소스의 lead_time_h·적재 기간을 확인하세요.",
        )

    aligned = aligned[aligned["lead_bucket_h"].isin(buckets)]

    metrics_list: list[PopBucketMetrics] = []
    rel_paths: list[Path] = []
    summary_rows: list[dict[str, object]] = []

    for bucket in sorted(buckets):
        for source in compare_sources:
            m = compute_bucket_metrics(
                aligned,
                source=source,
                lead_bucket_h=bucket,
                min_sample=min_sample,
            )
            metrics_list.append(m)
            summary_rows.append(metrics_to_summary_row(m))
            path = write_reliability_csv(m, reports_dir)
            if path is not None:
                rel_paths.append(path)

    summary = pd.DataFrame(summary_rows)
    return PopReportResult(
        summary=summary,
        bucket_metrics=tuple(metrics_list),
        reliability_paths=tuple(rel_paths),
        message=None,
    )


def format_summary_table(summary: pd.DataFrame) -> str:
    if summary.empty:
        return "(비어 있음)"
    display = summary.copy()
    for col in ("brier", "bss"):
        if col in display.columns:
            display[col] = display[col].map(
                lambda v: f"{v:.4f}" if isinstance(v, float) else str(v)
            )
    return display.to_string(index=False)


def resolve_report_window(
    data_dir: Path,
    *,
    start_date: date | None,
    end_date: date | None,
    forecast_sources: tuple[str, ...],
) -> tuple[date | None, date | None]:
    """명시 기간이 없으면 ASOS·예보 소스 파티션의 합집합에서 추론."""
    if start_date is not None and end_date is not None:
        return start_date, end_date

    scan_sources = (SOURCE_GROUND_TRUTH_ASOS, *forecast_sources)
    inferred_start, inferred_end = infer_date_range(data_dir, scan_sources)
    return (
        start_date or inferred_start,
        end_date or inferred_end,
    )
