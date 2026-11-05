import datetime as dt
from typing import Annotated

from pydantic import BeforeValidator, Field

from app.core.schemas import CamelModel
from app.modules.dashboard.application.dashboard_query_service import (
    DashboardSummary,
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


class DashboardSummaryResponse(CamelModel):
    """GET /api/v1/dashboard/summary 응답 (명세 4.4). 기준 시각은 lastSyncedAt 이고 서버의 지금 시각이 아니다 (C22)."""

    last_synced_at: dt.datetime | None = Field(
        description=(
            "기준 시각 T: 고객사의 마지막 적재 수집(SUCCEEDED·PARTIAL_SUCCESS) 완료 시각(UTC). "
            "적재된 수집이 없으면 null (건수 0, 비율·비교값 null)"
        ),
        examples=["2026-07-01T00:04:30Z"],
    )
    in_transit_shipment_count: int = Field(
        description="진행 중인 선적: current_stage 가 RECEIVED 가 아닌 선적 수",
        examples=[5],
    )
    week_over_week: int | None = Field(
        description=(
            "진행 중인 선적의 T − 7일 대비 증감 건수. T − 7일 값은 그때까지 관측된 마지막 일정 스냅샷의 단계로 센다"
        ),
        examples=[-1],
    )
    delayed_shipment_count: int = Field(
        description=(
            "지연 선적: 열린 에피소드 중 현재 실행이 NO_DELAY 가 아니고 최종 선택 전인 지연의 선적 수"
        ),
        examples=[2],
    )
    day_over_day: int | None = Field(
        description="지연 선적의 T − 1일 대비 증감 건수", examples=[1]
    )
    on_time_inbound_rate: float | None = Field(
        description=(
            "정시 입고율(0~1): (기준일 − 30일, 기준일]에 입고된 선적 중 actual ≤ planned 비율. 입고 0건이면 null"
        ),
        examples=[0.75],
    )
    month_over_month: float | None = Field(
        description=(
            "정시 입고율의 직전 30일 (기준일 − 60일, 기준일 − 30일] 대비 차이(0~1 단위). 어느 쪽이든 null 이면 null"
        ),
        examples=[-0.05],
    )
    at_risk_order_count: int = Field(
        description=(
            "위험 주문: 열린 에피소드 중 현재 실행이 READY_FOR_REVIEW 인 지연들의 위험 주문 수. "
            "상세 행이 아니라 서로 다른 주문 수(D21). 대상 실행이 없으면 0"
        ),
        examples=[3],
    )
    at_risk_order_rate: float | None = Field(
        description=(
            "위험 주문 비율(0~1): 미출고 주문(OPEN·PARTIALLY_SHIPPED) 대비. 미출고 주문이 0건이면 null"
        ),
        examples=[0.017],
    )

    @classmethod
    def of(cls, summary: DashboardSummary) -> DashboardSummaryResponse:
        return cls(
            last_synced_at=summary.last_synced_at,
            in_transit_shipment_count=summary.in_transit_shipment_count,
            week_over_week=summary.week_over_week,
            delayed_shipment_count=summary.delayed_shipment_count,
            day_over_day=summary.day_over_day,
            on_time_inbound_rate=summary.on_time_inbound_rate,
            month_over_month=summary.month_over_month,
            at_risk_order_count=summary.at_risk_order_count,
            at_risk_order_rate=summary.at_risk_order_rate,
        )
