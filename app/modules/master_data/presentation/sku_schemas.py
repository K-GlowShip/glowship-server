import uuid

from pydantic import Field

from app.core.pagination import Page
from app.core.schemas import CamelModel
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain
from app.modules.master_data.application.sku_list_service import (
    SkuListItem,
    SkuListResult,
    SkuValidationStatus,
)
from app.modules.master_data.infrastructure.master_data_enums import (
    MappingStatus,
    PopularityTier,
    SkuStatus,
)

MAX_QUERY_LENGTH = 100


class ExternalCodeResponse(CamelModel):
    """원천 시스템 하나가 이 상품을 부르는 코드 (명세 4.3 externalCodes[]). /warehouses 도 같은 모양을 쓴다."""

    source_domain: SourceDomain = Field(
        description="코드를 쓰는 원천 유형 (INVENTORY = WMS, SHIPMENT = TMS)",
        examples=["INVENTORY"],
    )
    external_code: str = Field(
        description="원천 시스템의 코드", examples=["WMS-SKU-001"]
    )
    mapping_status: MappingStatus = Field(
        description="PENDING·MAPPED·UNMAPPED. MAPPED 가 아니면 validationStatus 가 CODE_MISSING",
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
        description="원천별 외부 코드. sourceDomain 순. 매핑이 없으면 []"
    )
    validation_status: SkuValidationStatus = Field(
        description=(
            "OK·CODE_MISSING(MAPPED 아닌 매핑 있음)·ISSUE_OPEN(이 SKU 코드로 열린 검증 오류 있음). "
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
