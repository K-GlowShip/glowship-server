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


class RunSummary(CamelModel):
    run_id: uuid.UUID
    run_code: str = Field(
        description="표시용 실행 ID (D14, AN-YYMMDD-NNN). analysis_runs.run_code (0006, NOT NULL)"
    )
    status: str
    result_class: str | None = Field(
        description="결과 분류. 완료 판정이 READY_FOR_REVIEW·NO_RISK 일 때만 값이 있고, 완료 전·NO_DELAY·ON_HOLD·FAILED 이면 null (DDL ck21·ck22)"
    )
    failure_code: str | None = Field(
        description="실패 코드 (analysis_runs.failure_code, 명세 C27). ON_HOLD·FAILED 일 때만 값이 있고 그 밖의 상태는 null (DDL ck10·ck11)"
    )


class UserSummary(CamelModel):
    """사용자 요약. 키 이름은 역할 이름만 쓴다(예: author, decidedBy, 응답 본문 규칙 5.2)"""

    user_id: uuid.UUID
    display_name: str
    role_code: str = Field(description="CLIENT_MEMBER·CLIENT_LEAD·ADMIN")


class ResponseOptionSummary(CamelModel):
    response_option_id: uuid.UUID
    option_type: str = Field(
        description="KEEP_PLAN·PRE_DEPARTURE_AIR·KR_AIR_REPLENISH·OTHER_REGION_FULFILL"
    )
