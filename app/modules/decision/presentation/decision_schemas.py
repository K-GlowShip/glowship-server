import uuid
from datetime import datetime
from enum import Enum

from pydantic import Field

from app.core.pagination import Page
from app.core.schemas import CamelModel
from app.core.summary_schemas import (
    RunSummary,
    ShipmentSummary,
    SkuSummary,
    UserSummary,
)
from app.modules.decision.application.decision_list_service import (
    DecisionListItem,
    DecisionListResult,
)

MAX_QUERY_LENGTH = 100


class DecisionType(str, Enum):
    """판단 유형. 어느 표에서 온 행인지 (member_drafts / final_decisions)"""

    DRAFT = "DRAFT"
    FINAL = "FINAL"


class DecisionStatus(str, Enum):
    """판단 이력 상태 (명세 2절 표시명: 초안 / 최종 선택 / 재분석 필요)"""

    DRAFT = "DRAFT"
    FINAL = "FINAL"
    NEEDS_REANALYSIS = "NEEDS_REANALYSIS"


class ResponseOptionSummary(CamelModel):
    """대응안 요약 객체 (응답 본문 규칙 5.2)"""

    response_option_id: uuid.UUID
    option_type: str = Field(
        description="KEEP_PLAN·PRE_DEPARTURE_AIR·KR_AIR_REPLENISH·OTHER_REGION_FULFILL. 표시명은 라벨 사전 labels/ko-v1",
        examples=["KR_AIR_REPLENISH"],
    )


class DecisionListItemResponse(CamelModel):
    """의사결정 이력 목록의 한 행 (명세 4.7 GET /decisions). 초안과 최종 선택이 같은 모양이다.
    선택 이유·의견 본문과 당시 분석 수치는 없다. 행을 눌러 GET /analysis-runs/{runId} 로 본다."""

    decision_type: DecisionType = Field(
        description="DRAFT(팀원 초안)·FINAL(팀장 최종 선택). 화면 열은 없고 status 가 대신한다"
    )
    decision_id: uuid.UUID = Field(
        description="member_draft_id 또는 final_decision_id. 정렬 동점 규칙에 쓴다"
    )
    decided_at: datetime = Field(
        description="초안은 작성 시각(member_drafts.created_at), 최종 선택은 선택 시각(final_decisions.decided_at). UTC",
        examples=["2026-01-02T04:00:00Z"],
    )
    shipment: ShipmentSummary = Field(description="판단 대상 지연의 선적")
    representative_sku: SkuSummary | None = Field(
        description=(
            "대표 상품. 선적에서 수량이 가장 많은 SKU, 같으면 상세 줄 번호가 작은 SKU (D13, 지연 목록과 같은 계산). "
            "상세 줄이 없으면 null"
        )
    )
    response_option: ResponseOptionSummary | None = Field(
        description="고른 대응안. 의견만 쓴 초안이면 null (member_drafts_ck1). 최종 선택은 항상 있다"
    )
    decided_by: UserSummary = Field(
        description="초안은 작성자(CLIENT_MEMBER), 최종 선택은 선택자(CLIENT_LEAD)"
    )
    status: DecisionStatus = Field(
        description=(
            "FINAL(최종 선택) / DRAFT(초안이고 실행이 STALE 아님) / NEEDS_REANALYSIS(초안이고 실행이 STALE). "
            "저장값이 아니라 조회 때 계산한다 (명세 4.7 상태 표)"
        )
    )
    run: RunSummary = Field(
        description="판단한 분석 실행. runCode 가 화면의 실행 ID 열 (초안 행에도 표시, 명세 U6)"
    )
    delay_event_id: uuid.UUID = Field(
        description="지연 대응 센터로 이동할 때 쓰는 지연 ID"
    )

    @classmethod
    def of(cls, item: DecisionListItem) -> DecisionListItemResponse:
        representative_sku = (
            SkuSummary(
                sku_id=item.representative_sku_id,
                sku_code=item.representative_sku_code,
                product_name=item.representative_product_name,
            )
            if item.representative_sku_id is not None
            else None
        )
        response_option = (
            ResponseOptionSummary(
                response_option_id=item.response_option_id,
                option_type=item.option_type,
            )
            if item.response_option_id is not None
            else None
        )
        return cls(
            decision_type=item.decision_type,
            decision_id=item.decision_id,
            decided_at=item.decided_at,
            shipment=ShipmentSummary(
                shipment_id=item.shipment_id, shipment_code=item.shipment_code
            ),
            representative_sku=representative_sku,
            response_option=response_option,
            decided_by=UserSummary(
                user_id=item.user_id,
                display_name=item.display_name,
                role_code=item.role_code,
            ),
            status=item.status,
            run=RunSummary(
                run_id=item.run_id,
                run_code=item.run_code,
                status=item.run_status,
                result_class=item.run_result_class,
                failure_code=item.run_failure_code,
            ),
            delay_event_id=item.delay_event_id,
        )


class DecisionListResponse(Page[DecisionListItemResponse]):
    """GET /api/v1/decisions 응답. 공통 목록 모양(items·page·size·totalCount·totalPages)"""

    @classmethod
    def from_result(cls, result: DecisionListResult) -> DecisionListResponse:
        return cls.of(
            [DecisionListItemResponse.of(item) for item in result.items],
            result.total_count,
            result.page,
        )
