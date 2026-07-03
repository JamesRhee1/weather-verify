"""강수량 이진 변환 — Brier score 등 이진 지표용."""

from __future__ import annotations

import pandas as pd

PRECIP_BINARY_THRESHOLD_MM = 0.1


def precip_to_binary(mm: float, threshold: float = PRECIP_BINARY_THRESHOLD_MM) -> int:
    """강수량(mm)을 이진 강수 여부로 변환 (≥ threshold → 1, 미만 → 0)."""
    return 1 if mm >= threshold else 0


def precip_series_to_binary(
    series: pd.Series,
    threshold: float = PRECIP_BINARY_THRESHOLD_MM,
) -> pd.Series:
    """Series 단위 이진 변환."""
    return series.map(lambda v: precip_to_binary(float(v), threshold=threshold))
