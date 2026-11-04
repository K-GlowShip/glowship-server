import datetime as dt
from typing import Annotated

from pydantic import BeforeValidator, Field

from app.core.schemas import CamelModel
from app.modules.dashboard.application.dashboard_query_service import (
    InboundTrendDays,
    InboundTrendPoint,
    InboundTrendResult,
)

_DAYS_VALUES = {str(int(days)) for days in InboundTrendDays}


def _require_days_literal(value: object) -> object:
    """days 는 "7"·"30"·"90" 글자 그대로만 받는다. IntEnum 은 lax 모드라 "7.0"·"07"·"+7" 도 7 로 바꿔 받는다."""
    if isinstance(value, str) and value not in _DAYS_VALUES:
        raise ValueError("days 는 7·30·90 중 하나여야 합니다")
    return value


DaysQuery = Annotated[InboundTrendDays, BeforeValidator(_require_days_literal)]


class InboundTrendPointResponse(CamelModel):
    date: dt.date = Field(description="날짜 (UTC)", examples=["2026-06-30"])
    planned_count: int = Field(
        description="그날이 계획 3PL 입고일(planned_3pl_inbound_date)인 선적 수",
        examples=[2],
    )
    actual_count: int = Field(
        description="그날이 실제 3PL 입고일(actual_3pl_inbound_date)인 선적 수",
        examples=[1],
    )

    @classmethod
    def of(cls, point: InboundTrendPoint) -> InboundTrendPointResponse:
        return cls(
            date=point.day,
            planned_count=point.planned_count,
            actual_count=point.actual_count,
        )


class InboundTrendResponse(CamelModel):
    """GET /api/v1/dashboard/inbound-trend 응답 (명세 4.4). 단위는 건(선적 수)."""

    points: list[InboundTrendPointResponse] = Field(
        description=(
            "[기준일 − days + 1일, 기준일]의 날짜마다 한 점, 날짜 순. 0건인 날도 0. "
            "기준일 = 고객사 마지막 수집 완료 시각의 UTC 날짜(C22). 적재된 수집이 없으면 []"
        )
    )

    @classmethod
    def from_result(cls, result: InboundTrendResult) -> InboundTrendResponse:
        return cls(points=[InboundTrendPointResponse.of(p) for p in result.points])
