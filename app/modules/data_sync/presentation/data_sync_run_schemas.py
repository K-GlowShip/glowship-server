from datetime import datetime

from pydantic import Field

from app.core.schemas import CamelModel
from app.modules.data_sync.application.data_source_catalog import ExternalSystem
from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain


class ExternalSystemSyncStatusResponse(CamelModel):
    """외부 시스템 한 곳의 동기화 상태 (연동 데이터 조회 화면의 카드 한 장)"""

    source_domain: SourceDomain = Field(
        description="수집 도메인. 카드를 구분하는 키", examples=["ORDER"]
    )
    system_name: ExternalSystem = Field(
        description="연동한 외부 시스템 종류 (ORDER→ERP, INVENTORY→WMS, SHIPMENT→TMS)",
        examples=["ERP"],
    )
    display_name: str = Field(description="카드 제목", examples=["주문 데이터"])
    is_synced: bool = Field(
        description=(
            "동기화된 상태인지. 마지막으로 끝난 수집이 성공(부분 성공 포함)이고 "
            "마지막 수집 시작이 갱신 주기 × 배수 이내이면 true. 수집 이력이 없으면 false"
        ),
        examples=[True],
    )
    last_synced_at: datetime | None = Field(
        description="데이터가 마지막으로 반영된 시각(UTC). 마지막 성공 수집의 종료 시각. 성공한 적이 없으면 null",
        examples=["2026-11-02T03:15:00Z"],
    )
    update_interval_seconds: int = Field(
        description="자동 수집 주기(초)", examples=[600]
    )


class LatestDataSyncRunsResponse(CamelModel):
    """GET /api/v1/data-sync-runs/latest 응답"""

    external_systems: list[ExternalSystemSyncStatusResponse] = Field(
        description="외부 시스템별 동기화 상태. 항상 ORDER·INVENTORY·SHIPMENT 순서로 세 항목"
    )
