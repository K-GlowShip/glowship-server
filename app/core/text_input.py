from typing import Annotated

from pydantic import AfterValidator

"""
    자유 입력 문자열 공통 규칙 (이슈 #49, PR #46 리뷰 후속)
      - PostgreSQL text 는 NUL 문자(\\x00)를 저장·비교할 수 없어, 그대로 DB 에 보내면 500 이 납니다.
        저장하는 값(의견·선택 이유·재분석 이유)과 검색어(q)가 모두 같습니다.
      - NUL 이 있으면 지우지 않고 거부합니다(422 VALIDATION_ERROR). 입력을 몰래 바꾸면 최종 선택의
        같은 Idempotency-Key 본문 비교가 흔들립니다.
      - clean_free_text: 판단 API 자유 입력(명세 C25·C41)의 공통 순서 = NUL 거부 → 앞뒤 공백 제거 → 빈 값 → 길이
"""

NUL = "\x00"


def reject_nul(value: str, label: str) -> str:
    if NUL in value:
        raise ValueError(f"{label}: NUL 문자(\\0)를 쓸 수 없습니다.")
    return value


def clean_free_text(
    value: str, *, label: str, max_length: int, blank_is_none: bool = False
) -> str | None:
    """
    앞뒤 공백을 뺀 값을 돌려준다. 비었으면 blank_is_none 이면 None, 아니면 거부한다.
    오류는 ValueError 라 pydantic 검증기 안에서 부르면 422 VALIDATION_ERROR 가 된다.
    """
    reject_nul(value, label)
    stripped = value.strip()
    if not stripped:
        if blank_is_none:
            return None
        raise ValueError(f"{label}: 비우거나 공백만으로 쓸 수 없습니다.")
    if len(stripped) > max_length:
        raise ValueError(f"{label}: {max_length}자 이하여야 합니다.")
    return stripped


def _search_text(value: str | None) -> str | None:
    return None if value is None else reject_nul(value, "검색어")


# 목록 API 검색어(q). Annotated 안에 Query 를 넣어 쓴다: q: Annotated[SearchText, Query(max_length=...)] = None
# (q: SearchText = Query(...) 처럼 기본값 자리에 Query 를 두면 FastAPI 0.162 가 이 검증기를 빼고 검증한다. 11/09 확인)
SearchText = Annotated[str | None, AfterValidator(_search_text)]
