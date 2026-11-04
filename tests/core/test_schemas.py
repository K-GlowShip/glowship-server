from datetime import date

import pytest
from pydantic import TypeAdapter, ValidationError

from app.core.schemas import IsoDate

_adapter = TypeAdapter(IsoDate)


def test_YYYY_MM_DD_형식은_날짜로_받는다():
    assert _adapter.validate_python("2026-06-30") == date(2026, 6, 30)


@pytest.mark.parametrize(
    "value",
    [
        "1782777600",  # Unix 초
        "0",
        "2026-06-30T00:00:00Z",  # 자정 datetime
        "20260630",
        "2026-6-30",
        "2026-13-01",  # 형식은 맞지만 월 범위 밖 → pydantic date 검증이 거부
    ],
)
def test_YYYY_MM_DD_형식이_아니면_거부한다(value):
    with pytest.raises(ValidationError):
        _adapter.validate_python(value)
