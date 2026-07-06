"""임시 Streamlit 대시보드 — 적재 parquet 내부 확인용.

실행: streamlit run app.py
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from src.core.report import (
    DEFAULT_FORECAST_SOURCES,
    PopReportResult,
    build_pop_report,
    discover_issue_dates,
    infer_date_range,
    load_parquet_for_source,
)
from src.schema import (
    SOURCE_GROUND_TRUTH_ASOS,
    SOURCE_KMA_VILAGE,
    SOURCE_OPENMETEO_ECMWF,
    SOURCE_OPENMETEO_GFS,
)
from src.sources.kma_auth import ROOT

DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
CACHE_TTL_SEC = 600

STALE_AFTER_HOURS: dict[str, float] = {
    SOURCE_KMA_VILAGE: 3.0,
    SOURCE_OPENMETEO_ECMWF: 6.0,
    SOURCE_OPENMETEO_GFS: 6.0,
    SOURCE_GROUND_TRUTH_ASOS: 24.0,
}
MONITORED_SOURCES: tuple[str, ...] = tuple(STALE_AFTER_HOURS.keys())
PREVIEW_SOURCES: tuple[str, ...] = MONITORED_SOURCES


def scan_collection_status(
    data_dir: Path,
    *,
    now: datetime | None = None,
) -> pd.DataFrame:
    """data/ 파티션 스캔 — 소스별 수집 현황 (순수 pandas, UI·테스트 공용)."""
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows: list[dict[str, object]] = []

    for source in MONITORED_SOURCES:
        issue_dates = discover_issue_dates(data_dir, source)
        frame = load_parquet_for_source(data_dir, source) if issue_dates else pd.DataFrame()

        recent_issue_time: pd.Timestamp | None = None
        if not frame.empty and "issue_time" in frame.columns:
            recent_issue_time = pd.to_datetime(frame["issue_time"], utc=True).max()

        elapsed: timedelta | None = None
        if recent_issue_time is not None and pd.notna(recent_issue_time):
            recent_dt = recent_issue_time.to_pydatetime()
            if recent_dt.tzinfo is None:
                recent_dt = recent_dt.replace(tzinfo=timezone.utc)
            elapsed = now_utc - recent_dt.astimezone(timezone.utc)

        threshold_h = STALE_AFTER_HOURS[source]
        stale = elapsed is None or elapsed > timedelta(hours=threshold_h)
        elapsed_hours = round(elapsed.total_seconds() / 3600, 1) if elapsed else None

        rows.append(
            {
                "source": source,
                "recent_issue_time": recent_issue_time,
                "total_rows": len(frame),
                "issue_date_count": len(issue_dates),
                "elapsed_hours": elapsed_hours,
                "stale_threshold_h": threshold_h,
                "stale": stale,
            }
        )

    return pd.DataFrame(rows)


def _load_source_impl(data_dir_str: str, source: str, variable: str | None) -> pd.DataFrame:
    var = None if variable == "" else variable
    return load_parquet_for_source(Path(data_dir_str), source, variable=var)


def _pop_report_impl(
    data_dir_str: str,
    reports_dir_str: str,
    start_iso: str | None,
    end_iso: str | None,
    truth_mode: str,
    lead_bucket_hours: int,
) -> PopReportResult:
    start = date.fromisoformat(start_iso) if start_iso else None
    end = date.fromisoformat(end_iso) if end_iso else None
    return build_pop_report(
        data_dir=Path(data_dir_str),
        reports_dir=Path(reports_dir_str),
        start_date=start,
        end_date=end,
        truth_mode=truth_mode,  # type: ignore[arg-type]
        lead_bucket_hours=lead_bucket_hours,
    )


def _format_status_table(status: pd.DataFrame) -> pd.DataFrame:
    display = status.copy()
    if "recent_issue_time" in display.columns:
        display["recent_issue_time"] = display["recent_issue_time"].apply(
            lambda ts: ts.isoformat() if pd.notna(ts) else "—"
        )
    display["elapsed"] = display.apply(
        lambda r: f"{r['elapsed_hours']}h" if pd.notna(r["elapsed_hours"]) else "—",
        axis=1,
    )
    return display[
        ["source", "recent_issue_time", "total_rows", "issue_date_count", "elapsed", "stale"]
    ]


def _render_reliability_chart(st, rel: pd.DataFrame) -> None:
    if rel.empty:
        st.info("reliability 데이터가 없습니다.")
        return
    try:
        import plotly.graph_objects as go

        sizes = rel["n"].clip(lower=1) ** 0.5 * 8
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=rel["forecast_mean"],
                y=rel["observed_freq"],
                mode="markers",
                marker={"size": sizes, "opacity": 0.85},
                text=[f"n={n}" for n in rel["n"]],
                hovertemplate="예보=%{x:.2f}<br>관측=%{y:.2f}<extra></extra>",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=[0, 1],
                y=[0, 1],
                mode="lines",
                line={"dash": "dash", "color": "gray"},
                name="perfect",
            )
        )
        fig.update_layout(
            xaxis_title="예보 확률 (평균)",
            yaxis_title="관측 빈도",
            xaxis={"range": [0, 1]},
            yaxis={"range": [0, 1]},
            height=420,
            showlegend=False,
        )
        st.plotly_chart(fig, use_container_width=True)
    except ImportError:
        st.scatter_chart(rel, x="forecast_mean", y="observed_freq", size="n")


def main() -> None:
    import streamlit as st

    cached_load_source = st.cache_data(ttl=CACHE_TTL_SEC)(_load_source_impl)
    cached_pop_report = st.cache_data(ttl=CACHE_TTL_SEC)(_pop_report_impl)

    st.set_page_config(page_title="weather-verify", layout="wide")
    st.title("weather-verify — 임시 대시보드")
    st.caption("cron 적재 parquet 내부 확인용 (과제·검증 로직은 report.py / CLI 사용)")

    st.header("수집 현황")
    try:
        status = scan_collection_status(DATA_DIR)
        st.dataframe(_format_status_table(status), use_container_width=True, hide_index=True)
        for _, row in status.iterrows():
            if row["stale"]:
                label = row["source"]
                threshold = row["stale_threshold_h"]
                elapsed = row["elapsed_hours"]
                if elapsed is None:
                    st.warning(f"{label}: 데이터 없음 — cron 확인 필요 (기준 {threshold}h)")
                else:
                    st.warning(
                        f"{label}: 마지막 수집 후 {elapsed}h 경과 — "
                        f"cron 확인 필요 (기준 {threshold}h)"
                    )
    except Exception as exc:
        st.info(f"수집 현황을 읽을 수 없습니다: {exc}")

    scan_sources = (SOURCE_GROUND_TRUTH_ASOS, *DEFAULT_FORECAST_SOURCES)
    inferred_start, inferred_end = infer_date_range(DATA_DIR, scan_sources)
    default_start = inferred_start or date.today()
    default_end = inferred_end or date.today()

    with st.sidebar:
        st.header("POP 리포트")
        start_date = st.date_input("시작 issue_date", value=default_start)
        end_date = st.date_input("종료 issue_date", value=default_end)
        truth_mode = st.selectbox("truth 방식", ["window_3h", "point"], index=0)
        lead_bucket_hours = st.number_input("리드 버킷 (시간)", min_value=1, value=6, step=1)

    st.header("POP 리포트")
    report: PopReportResult | None = None
    try:
        if start_date > end_date:
            st.info("시작일이 종료일보다 늦습니다.")
        else:
            report = cached_pop_report(
                str(DATA_DIR),
                str(REPORTS_DIR),
                start_date.isoformat(),
                end_date.isoformat(),
                truth_mode,
                int(lead_bucket_hours),
            )
    except Exception as exc:
        st.info(f"POP 리포트를 생성할 수 없습니다: {exc}")

    if report is not None:
        if report.message:
            st.info(report.message)
        if not report.summary.empty:
            st.dataframe(report.summary, use_container_width=True, hide_index=True)
        elif report.message is None:
            st.info("표시할 summary 가 없습니다.")

    st.header("Reliability diagram")
    if report is None or not report.bucket_metrics:
        st.info("reliability diagram 을 그릴 데이터가 없습니다.")
    else:
        options = [
            m
            for m in report.bucket_metrics
            if not m.sample_insufficient and not m.reliability.empty
        ]
        if not options:
            st.info("표본이 충분한 reliability 버킷이 없습니다 (n ≥ 30 필요).")
        else:
            labels = [f"{m.source} / lead {m.lead_bucket_h}h (n={m.n})" for m in options]
            choice = st.selectbox("소스 · 리드 버킷", labels, index=0)
            selected = options[labels.index(choice)]
            _render_reliability_chart(st, selected.reliability)

    with st.expander("원시 데이터 미리보기"):
        preview_source = st.selectbox("소스", PREVIEW_SOURCES, key="preview_source")
        try:
            preview = cached_load_source(str(DATA_DIR), preview_source, "")
            if preview.empty:
                st.info(f"{preview_source} parquet 가 비어 있습니다.")
            else:
                st.dataframe(preview.tail(200), use_container_width=True, hide_index=True)
        except Exception as exc:
            st.info(f"미리보기 로드 실패: {exc}")


if __name__ == "__main__":
    main()
