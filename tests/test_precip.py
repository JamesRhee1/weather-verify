"""강수 이진 변환 단위 테스트."""

from __future__ import annotations

import pandas as pd
import pytest
from src.core.precip import PRECIP_BINARY_THRESHOLD_MM, precip_series_to_binary, precip_to_binary


def test_precip_to_binary_threshold():
    assert precip_to_binary(0.0) == 0
    assert precip_to_binary(0.09) == 0
    assert precip_to_binary(0.1) == 1
    assert precip_to_binary(5.0) == 1


def test_precip_to_binary_custom_threshold():
    assert precip_to_binary(0.5, threshold=1.0) == 0
    assert precip_to_binary(1.0, threshold=1.0) == 1


def test_precip_series_to_binary():
    series = pd.Series([0.0, 0.1, 2.5])
    result = precip_series_to_binary(series)
    assert result.tolist() == [0, 1, 1]
    assert PRECIP_BINARY_THRESHOLD_MM == pytest.approx(0.1)
