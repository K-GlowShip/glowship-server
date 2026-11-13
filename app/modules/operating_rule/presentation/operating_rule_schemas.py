import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import Field

from app.core.schemas import CamelModel, IsoDate
from app.core.summary_schemas import WarehouseSummary
from app.modules.operating_rule.application.operating_rule_service import (
    CreatedRuleSet,
    RuleSetDetail,
)
from app.modules.operating_rule.infrastructure.repository.operating_rule_repository import (
    LeadTimeRow,
    NewLeadTime,
)

"""
    운영 기준 API 요청·응답 스키마 (API 4·5, API 명세 v6.2 4.2절)
      - 유형·기준·권역 값과 숫자 범위는 형식 오류(422 VALIDATION_ERROR)로 거릅니다.
        유형별 toRegionCode 규칙·같은 키 중복·다른 고객사 창고는 서비스가 400 INVALID_LEAD_TIME 으로 거부합니다(명세 4.2 오류).
      - 조회 응답의 leadTimes 는 명세 4.2 응답 항목에 없는 추가 필드입니다(응답 본문 규칙 7절 "추가는 자유", 11/13 결정).
        Admin 화면이 없어 저장한 리드타임을 API 로 확인할 수 있어야 하기 때문입니다.
"""

LeadTimeTypeCode = Literal[
    "CUSTOMER_DELIVERY", "AIR_TRANSIT", "INBOUND_HANDLING", "OUTBOUND_HANDLING"
]
DayBasisCode = Literal["CALENDAR_DAY", "BUSINESS_DAY"]
# 창고 권역(warehouses_ck2)과 같은 값
RegionCode = Literal["KR", "WEST", "EAST"]

# integer 열 상한. 넘는 값이 DB 까지 가면 500 이라 형식 오류로 거른다
_MAX_DAYS = 3650


class LeadTimeRequest(CamelModel):
    lead_time_type: LeadTimeTypeCode = Field(examples=["AIR_TRANSIT"])
    warehouse_id: uuid.UUID = Field(description="이 고객사의 창고")
    to_region_code: RegionCode | None = Field(
        default=None,
        description="도착 권역. CUSTOMER_DELIVERY·AIR_TRANSIT 는 필수, 그 밖의 유형은 넣지 않는다",
        examples=["WEST"],
    )
    days: int = Field(ge=0, le=_MAX_DAYS, examples=[5])
    day_basis: DayBasisCode = Field(
        description="일수 기준. 분석 계산은 CALENDAR_DAY 만 지원하며, BUSINESS_DAY 가 계산에 쓰이면 그 분석은 보류(ON_HOLD)된다",
        examples=["CALENDAR_DAY"],
    )

    def to_new(self) -> NewLeadTime:
        return NewLeadTime(
            lead_time_type=self.lead_time_type,
            warehouse_id=self.warehouse_id,
            to_region_code=self.to_region_code,
            days=self.days,
            day_basis=self.day_basis,
        )


class OperatingRuleSetCreateRequest(CamelModel):
    """운영 기준 새 버전 저장 요청. 주문 배정 순서는 고정 규칙이라 입력 항목이 아니다(명세 4.2)."""

    company_id: uuid.UUID = Field(
        description="운영 기준을 저장할 고객사. 요청자가 배정된 고객사여야 한다.",
        examples=["1a2052c0-cbfc-523e-ac43-ff6e6d1dd9a6"],
    )
    effective_date: IsoDate = Field(
        description="적용 시작일 (YYYY-MM-DD). 이 날짜부터 분석과 최종 선택 재확인이 이 버전을 쓴다",
        examples=["2026-07-03"],
    )
    safety_stock_days: int = Field(gt=0, le=_MAX_DAYS, examples=[14])
    sales_basis_days: int = Field(
        gt=0,
        le=_MAX_DAYS,
        description="판매 기준 일수. 분석 계산은 28일만 지원하며, 다른 값이면 분석이 보류(ON_HOLD)된다",
        examples=[28],
    )
    is_partial_fulfillment_allowed: bool | None = Field(
        default=None, description="부분 출고 허용. null = 정책 미정"
    )
    lead_times: list[LeadTimeRequest] = Field(max_length=500)


class OperatingRuleSetCreateResponse(CamelModel):
    operating_rule_set_id: uuid.UUID
    version_no: int

    @classmethod
    def of(cls, created: CreatedRuleSet) -> OperatingRuleSetCreateResponse:
        return cls(
            operating_rule_set_id=created.operating_rule_set_id,
            version_no=created.version_no,
        )


class RuleSetAuthor(CamelModel):
    """작성자. 요청자에게 보이지 않는 사용자(다른 ADMIN)면 displayName·roleCode 가 null 이다(users RLS 정책)."""

    user_id: uuid.UUID
    display_name: str | None
    role_code: str | None


class LeadTimeResponse(CamelModel):
    lead_time_type: str
    warehouse: WarehouseSummary
    to_region_code: str | None
    days: int
    day_basis: str

    @classmethod
    def of(cls, row: LeadTimeRow) -> LeadTimeResponse:
        return cls(
            lead_time_type=row.lead_time_type,
            warehouse=WarehouseSummary(
                warehouse_id=row.warehouse_id,
                warehouse_code=row.warehouse_code,
                region_code=row.warehouse_region_code,
            ),
            to_region_code=row.to_region_code,
            days=row.days,
            day_basis=row.day_basis,
        )


class OperatingRuleSetResponse(CamelModel):
    operating_rule_set_id: uuid.UUID
    version_no: int
    effective_date: date
    safety_stock_days: int
    sales_basis_days: int
    is_partial_fulfillment_allowed: bool | None
    created_at: datetime
    author: RuleSetAuthor
    is_current: bool = Field(
        description="지금 적용 중인 버전 (적용 시작일이 지난 것 중 버전이 가장 큰 것). 목록에서 최대 하나"
    )
    lead_times: list[LeadTimeResponse]

    @classmethod
    def of(cls, detail: RuleSetDetail) -> OperatingRuleSetResponse:
        row = detail.row
        return cls(
            operating_rule_set_id=row.operating_rule_set_id,
            version_no=row.version_no,
            effective_date=row.effective_from,
            safety_stock_days=row.safety_stock_days,
            sales_basis_days=row.sales_basis_days,
            is_partial_fulfillment_allowed=row.allow_partial_fulfillment,
            created_at=row.created_at,
            author=RuleSetAuthor(
                user_id=row.author_user_id,
                display_name=row.author_display_name,
                role_code=row.author_role_code,
            ),
            is_current=detail.is_current,
            lead_times=[LeadTimeResponse.of(lt) for lt in detail.lead_times],
        )


class OperatingRuleSetListResponse(CamelModel):
    """버전 수가 적어 페이지를 나누지 않는다. 버전 내림차순."""

    items: list[OperatingRuleSetResponse]
