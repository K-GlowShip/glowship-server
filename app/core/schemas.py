import re
from datetime import date
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict
from pydantic.alias_generators import to_camel

"""
    요청·응답 스키마 공통 베이스

    API 명세(D2)에 따라 JSON 필드는 camelCase 로 주고받고, 파이썬 코드는 snake_case 를 유지한다.
      - alias_generator: snake_case 필드 이름 → camelCase 별칭
      - validate_by_alias / validate_by_name: 요청은 camelCase 로 받고, 파이썬 코드에서는 snake_case 로도 만들 수 있다
      - serialize_by_alias: 응답은 camelCase 로 직렬화한다 (FastAPI 의 response_model 도 별칭을 쓴다)
      - from_attributes: ORM 객체를 model_validate 로 바로 응답 스키마에 담을 수 있다
"""


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
        from_attributes=True,
    )


"""
    날짜 쿼리 파라미터 타입 (API 명세 2절 "날짜(date)는 YYYY-MM-DD")

    pydantic 의 date 는 lax 모드라 "1782777600"(Unix 초), "0", "2026-06-30T00:00:00Z"(자정 datetime)도
    날짜로 바꿔 받는다. 명세는 YYYY-MM-DD 만 허용하고 그 외는 422 이므로, pydantic 검증 전에 형식을 먼저 확인한다.
    형식이 맞으면 pydantic 의 기존 date 검증(월·일 범위 등)이 그대로 이어진다.
      - 라우터:  inventory_date: IsoDate | None = Query(default=None, alias="date")
"""

_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _require_iso_date(value: object) -> object:
    if isinstance(value, str) and not _ISO_DATE_PATTERN.match(value):
        raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다")
    return value


IsoDate = Annotated[date, BeforeValidator(_require_iso_date)]
