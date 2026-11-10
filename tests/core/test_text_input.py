import pytest
from pydantic import BaseModel, ValidationError

from app.core.text_input import SearchText, clean_free_text

"""
    자유 입력 공통 규칙 (이슈 #49) — DB 없음
"""


@pytest.mark.parametrize(
    ("value", "expected"),
    [("  의견  ", "의견"), ("가" * 600, "가" * 600), (" 가 나 ", "가 나")],
)
def test_clean_free_text_strips(value, expected):
    assert clean_free_text(value, label="의견", max_length=600) == expected


@pytest.mark.parametrize("value", ["\x00", "앞\x00뒤", "  \x00", "   ", "", "가" * 601])
def test_clean_free_text_rejects(value):
    with pytest.raises(ValueError):
        clean_free_text(value, label="의견", max_length=600)


def test_blank_is_none_only_without_nul():
    assert (
        clean_free_text(" \t", label="이유", max_length=600, blank_is_none=True) is None
    )
    with pytest.raises(ValueError, match="NUL"):
        clean_free_text(" \x00 ", label="이유", max_length=600, blank_is_none=True)


class _Query(BaseModel):
    q: SearchText = None


def test_search_text_rejects_nul_and_keeps_other_values():
    assert _Query(q="SKU-001").q == "SKU-001"
    assert _Query().q is None
    with pytest.raises(ValidationError, match="NUL"):
        _Query(q="SKU\x00")
