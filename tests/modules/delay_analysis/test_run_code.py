from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.modules.delay_analysis.application.run_creation.run_code import (
    MAX_DAILY_SEQ,
    basis_date,
    day_prefix,
    format_run_code,
    next_run_code,
)

KST = timezone(timedelta(hours=9))


def test_basis_date_is_utc_date_of_started_at():
    # KST 2026-01-02 08:00 = UTC 2026-01-01 23:00 → 기준일은 UTC 날짜
    assert basis_date(datetime(2026, 1, 2, 8, 0, tzinfo=KST)) == date(2026, 1, 1)
    assert basis_date(datetime(2026, 1, 2, 0, 5, tzinfo=UTC)) == date(2026, 1, 2)


def test_basis_date_rejects_naive_datetime():
    with pytest.raises(ValueError):
        basis_date(datetime(2026, 1, 2, 0, 5))


def test_format_run_code_uses_yymmdd_and_three_digit_seq():
    assert day_prefix(date(2026, 1, 2)) == "AN-260102-"
    assert format_run_code(date(2026, 1, 2), 3) == "AN-260102-003"
    with pytest.raises(ValueError):
        format_run_code(date(2026, 1, 2), 0)
    with pytest.raises(ValueError):
        format_run_code(date(2026, 1, 2), MAX_DAILY_SEQ + 1)


def test_next_run_code_starts_at_001_and_increments():
    basis = date(2026, 11, 4)
    assert next_run_code(basis, None) == "AN-261104-001"
    assert next_run_code(basis, "AN-261104-007") == "AN-261104-008"
    assert next_run_code(basis, "AN-261104-999") is None


def test_next_run_code_rejects_code_of_another_day():
    with pytest.raises(ValueError):
        next_run_code(date(2026, 11, 4), "AN-261103-005")
