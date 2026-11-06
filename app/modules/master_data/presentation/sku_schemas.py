import uuid
from datetime import datetime

from pydantic import Field

from app.core.pagination import Page
from app.core.schemas import CamelModel
from app.core.summary_schemas import RunSummary, ShipmentSummary
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain
from app.modules.master_data.application.sku_list_service import (
    SkuDetail,
    SkuListItem,
    SkuListResult,
    SkuValidationStatus,
)
from app.modules.master_data.infrastructure.master_data_enums import (
    MappingStatus,
    PopularityTier,
    SkuStatus,
)
from app.modules.master_data.infrastructure.repository.sku_repository import (
    AffectedDelayEventRow,
    SkuIssueRow,
)

MAX_QUERY_LENGTH = 100


class ExternalCodeResponse(CamelModel):
    """원천 시스템 하나가 이 상품을 부르는 코드 (명세 4.3 externalCodes[]). /warehouses 도 같은 모양을 쓴다.
    SKU 코드를 쓰는 원천(INVENTORY·SHIPMENT)마다 항목이 항상 하나 있고, 매핑이 없으면 externalCode 가 null 이다."""

    source_domain: SourceDomain = Field(
        description="코드를 쓰는 원천 유형 (INVENTORY = WMS, SHIPMENT = TMS)",
        examples=["INVENTORY"],
    )
    external_code: str | None = Field(
        description="원천 시스템의 코드. 이 원천에 MAPPED 매핑이 없으면 null",
        examples=["WMS-SKU-001"],
    )
    mapping_status: MappingStatus = Field(
        description=(
            "MAPPED(매핑 있음)·UNMAPPED(매핑 없음, externalCode = null). "
            "UNMAPPED 항목이 하나라도 있으면 validationStatus 가 CODE_MISSING. "
            "DB 의 PENDING·UNMAPPED 행은 SKU 에 붙지 않아 이 목록에 나오지 않는다"
        ),
        examples=["MAPPED"],
    )


class SkuListItemResponse(CamelModel):
    """상품 탭의 한 행 (명세 4.3 GET /skus)"""

    sku_id: uuid.UUID
    sku_code: str = Field(
        description="화면의 ERP 코드 (skus.sku_code)", examples=["SKU-001"]
    )
    product_name: str = Field(examples=["A Glow Hydra Toner Pad 60ea"])
    brand_name: str = Field(examples=["A Glow"])
    hs10: str = Field(
        description="HS 10자리 코드 (skus.hs10). 상품 상세 모달에 표시",
        examples=["3304995000"],
    )
    category: str = Field(examples=["SKINCARE"])
    subcategory: str = Field(examples=["Toner Pad"])
    popularity_tier: PopularityTier = Field(
        description="HIT·REGULAR·LONG_TAIL. 화면의 히트 상품은 HIT", examples=["HIT"]
    )
    is_emergency_air_eligible: bool = Field(description="긴급 항공 전환 가능 여부")
    unit_code: str = Field(examples=["EA"])
    units_per_case: int = Field(examples=[24])
    status: SkuStatus = Field(description="ACTIVE·INACTIVE", examples=["ACTIVE"])
    external_codes: list[ExternalCodeResponse] = Field(
        description=(
            "원천별 외부 코드. SKU 코드를 쓰는 원천(INVENTORY, SHIPMENT) 순으로 항목이 항상 하나씩 있고, "
            "매핑이 없는 원천은 externalCode = null, mappingStatus = UNMAPPED"
        )
    )
    validation_status: SkuValidationStatus = Field(
        description=(
            "OK·CODE_MISSING(매핑 없는 원천 있음)·ISSUE_OPEN(이 SKU 코드로 열린 검증 오류 있음). "
            "둘 다면 CODE_MISSING"
        ),
        examples=["OK"],
    )

    @classmethod
    def of(cls, item: SkuListItem) -> SkuListItemResponse:
        return cls(
            sku_id=item.sku_id,
            sku_code=item.sku_code,
            product_name=item.product_name,
            brand_name=item.brand_name,
            hs10=item.hs10,
            category=item.category,
            subcategory=item.subcategory,
            popularity_tier=item.popularity_tier,
            is_emergency_air_eligible=item.is_emergency_air_eligible,
            unit_code=item.unit_code,
            units_per_case=item.units_per_case,
            status=item.status,
            external_codes=[
                ExternalCodeResponse.model_validate(code)
                for code in item.external_codes
            ],
            validation_status=item.validation_status,
        )


class SkuListResponse(Page[SkuListItemResponse]):
    """GET /api/v1/skus 응답. 공통 목록 모양(items·page·size·totalCount·totalPages)"""

    @classmethod
    def from_result(cls, result: SkuListResult) -> SkuListResponse:
        return cls.of(
            [SkuListItemResponse.of(item) for item in result.items],
            result.total_count,
            result.page,
        )


class SkuIssueResponse(CamelModel):
    """이 상품 코드로 열린 재고 검증 이슈 (GET /skus 의 ISSUE_OPEN 과 같은 조건)"""

    data_validation_issue_id: uuid.UUID
    source_record_key: str = Field(examples=["WMS-EWR01|SKU-201|2026-01-01"])
    field_name: str | None = Field(examples=["warehouseCode"])
    error_code: str = Field(examples=["UNKNOWN_WAREHOUSE"])
    error_message: str
    severity: str = Field(description="ERROR·WARNING", examples=["ERROR"])
    detected_at: datetime

    @classmethod
    def of(cls, row: SkuIssueRow) -> SkuIssueResponse:
        return cls(
            data_validation_issue_id=row.data_validation_issue_id,
            source_record_key=row.source_record_key,
            field_name=row.field_name,
            error_code=row.error_code,
            error_message=row.error_message,
            severity=row.severity,
            detected_at=row.detected_at,
        )


class AffectedDelayEventResponse(CamelModel):
    """
    이 상품이 선적에 포함되고 현재 실행이 ON_HOLD 인 열린 지연.
    이 상품 때문에 보류됐다는 뜻은 아니다(다른 사유의 보류도 나온다).
    """

    delay_event_id: uuid.UUID
    shipment: ShipmentSummary
    current_run: RunSummary

    @classmethod
    def of(cls, row: AffectedDelayEventRow) -> AffectedDelayEventResponse:
        return cls(
            delay_event_id=row.delay_event_id,
            shipment=ShipmentSummary(
                shipment_id=row.shipment_id, shipment_code=row.shipment_code
            ),
            current_run=RunSummary(
                run_id=row.analysis_run_id,
                run_code=row.run_code,
                status=row.status,
                result_class=row.result_class,
                failure_code=row.failure_code,
            ),
        )


class SkuDetailResponse(SkuListItemResponse):
    """GET /api/v1/skus/{skuId} 응답. 목록 행과 같은 필드에 열린 이슈와 영향 지연을 더한다."""

    issues: list[SkuIssueResponse] = Field(
        description="이 상품 코드로 열린 재고 검증 이슈. detectedAt 최신순, 없으면 빈 배열"
    )
    affected_delay_events: list[AffectedDelayEventResponse] = Field(
        description="이 상품이 포함되고 현재 실행이 ON_HOLD 인 열린 지연. detectedAt 최신순, 없으면 빈 배열"
    )

    @classmethod
    def from_detail(cls, detail: SkuDetail) -> SkuDetailResponse:
        return cls(
            **SkuListItemResponse.of(detail.item).model_dump(),
            issues=[SkuIssueResponse.of(row) for row in detail.issues],
            affected_delay_events=[
                AffectedDelayEventResponse.of(row)
                for row in detail.affected_delay_events
            ],
        )
