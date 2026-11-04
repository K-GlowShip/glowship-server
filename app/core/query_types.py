import re
from typing import Annotated

from pydantic import AwareDatetime, BeforeValidator

"""
    쿼리 파라미터 공통 타입 (API 명세 v6.2 2절 시간)
      - 기간 필터 from·to 는 ISO 8601 시각 문자열만 받습니다. 시간대가 없으면 AwareDatetime 이 거부합니다.
      - AwareDatetime 은 "0"·"1782777600" 같은 숫자를 Unix 초(UTC)로 바꿔 받으므로, 날짜·시각 모양이 아니면 먼저 거부합니다.
      - 쓰는 곳: GET /orders, GET /delay-events (같은 규칙이라 응답도 같아야 함)
"""

_ISO_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T")


def _require_iso_datetime(value: object) -> object:
    if isinstance(value, str) and not _ISO_DATETIME.match(value):
        raise ValueError("ISO 8601 시각이어야 합니다 (예: 2026-10-20T15:00:00Z)")
    return value


IsoAwareDatetime = Annotated[AwareDatetime, BeforeValidator(_require_iso_datetime)]
