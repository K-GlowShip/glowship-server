import uuid

from pydantic import Field

from app.core.schemas import CamelModel

"""
    응답에 넣는 연관 대상 요약 객체 (응답 본문 규칙 5.2, API 명세 2절 "연관 대상")
      - 화면에 함께 보여 줄 대상은 ID만 주지 않고 아래 모양으로 넣습니다. 어느 API에서든 같은 모양을 씁니다.
      - 필드를 더 넣어야 하면 응답 본문 규칙 5.2 표를 먼저 고친 뒤 모든 API에 똑같이 적용합니다.
"""


class ShipmentSummary(CamelModel):
    shipment_id: uuid.UUID
    shipment_code: str


class WarehouseSummary(CamelModel):
    warehouse_id: uuid.UUID
    warehouse_code: str
    region_code: str


class SkuSummary(CamelModel):
    sku_id: uuid.UUID
    sku_code: str
    product_name: str


class UserSummary(CamelModel):
    """사용자 요약 (작성자·선택자 등 역할 이름으로 담는다: author, decidedBy). 이메일은 넣지 않는다."""

    user_id: uuid.UUID
    display_name: str
    role_code: str = Field(
        description="OWNER·ADMIN·CLIENT_MEMBER(팀원)·CLIENT_LEAD(팀장)",
        examples=["CLIENT_LEAD"],
    )


class RunSummary(CamelModel):
    run_id: uuid.UUID
    run_code: str | None = Field(
        description="표시용 실행 ID (D14). analysis_runs.run_code 마이그레이션(0006) 전에는 null"
    )
    status: str
    result_class: str | None = Field(
        description="결과 분류. 완료 판정이 READY_FOR_REVIEW·NO_RISK 일 때만 값이 있고, 완료 전·NO_DELAY·ON_HOLD·FAILED 이면 null (DDL ck21·ck22)"
    )
