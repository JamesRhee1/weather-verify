"""kma_auth 유틸 단위 테스트."""

from __future__ import annotations

from src.sources.kma_auth import is_auth_failure


def test_is_auth_failure_service_key_message():
    assert is_auth_failure("30", "SERVICE_KEY_IS_NOT_REGISTERED_ERROR")


def test_is_auth_failure_key_and_error_requires_both():
    assert is_auth_failure("", "KEY ERROR in message")
    assert not is_auth_failure("", "API KEY missing")


def test_is_auth_failure_result_codes():
    assert is_auth_failure("30", "ok")
    assert not is_auth_failure("00", "NORMAL_SERVICE")
