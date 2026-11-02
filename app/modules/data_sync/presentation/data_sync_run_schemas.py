from datetime import datetime

from pydantic import Field

from app.core.schemas import CamelModel
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain


class ExternalSystemSyncStatusResponse(CamelModel):
    """원천 유형 하나의 동기화 상태 (연동 데이터 조회 화면의 카드 한 장)"""

    source_domain: SourceDomain = Field(
        description="원천 유형. 항목을 구분하는 키", examples=["ORDER"]
    )
    source_system_name: str = Field(
        description="원천 시스템 이름 (운영 설정 값)", examples=["SAP ERP"]
    )
    sync_interval_minutes: int = Field(
        description="자동 수집 주기(분, 운영 설정 값)", examples=[10]
    )
    is_synced: bool = Field(
        description=(
            "동기화된 상태인지. 마지막으로 끝난 수집이 성공(부분 성공 포함)이고 "
            "마지막 수집 시작이 수집 주기 × 배수 이내이면 true. 수집 이력이 없으면 false"
        ),
        examples=[True],
    )
    last_synced_at: datetime | None = Field(
        description="데이터가 마지막으로 반영된 시각(UTC). 마지막 성공 수집의 종료 시각. 성공한 적이 없으면 null",
        examples=["2026-11-02T03:15:00Z"],
    )


class LatestDataSyncRunsResponse(CamelModel):
    """GET /api/v1/data-sync-runs/latest 응답"""

    items: list[ExternalSystemSyncStatusResponse] = Field(
        description=(
            "원천 유형별 동기화 상태. 항상 ORDER·INVENTORY·SHIPMENT·PRODUCT·WAREHOUSE 순서로 다섯 항목"
        )
    )
