from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, model_validator

from app.core.schemas import CamelModel
from app.modules.data_sync.infrastructure.data_sync_enums import (
    ConnectionMethod,
    DataSyncRunStatus,
    SourceDomain,
    SourceSystem,
    SyncAdvance,
)


class DataSourceSyncStatusResponse(CamelModel):
    """
    원천 유형 하나의 마지막 수집 상태 (명세 v6.2 4.3)
      - dataSyncRunId ~ rejectedCount: 상태와 무관한 가장 최근 실행 1건. 수집 이력이 없으면 모두 null
      - sourceSystemName, syncIntervalMinutes: 운영 설정 값 (서버 카탈로그)
      - isSynced, lastSyncedAt: 명세 필드에 더한 서버 판정값
    """

    data_sync_run_id: uuid.UUID | None = Field(
        description="가장 최근 수집 실행 ID. 수집 이력이 없으면 null"
    )
    source_domain: SourceDomain = Field(
        description="원천 유형. 항목을 구분하는 키", examples=["ORDER"]
    )
    connection_method: ConnectionMethod | None = Field(
        description="가장 최근 실행의 연결 방식. 이력이 없으면 null", examples=["API"]
    )
    status: DataSyncRunStatus | None = Field(
        description=(
            "가장 최근 실행의 상태 (RUNNING·SUCCEEDED·PARTIAL_SUCCESS·FAILED). 이력이 없으면 null"
        ),
        examples=["SUCCEEDED"],
    )
    started_at: datetime | None = Field(
        description="가장 최근 실행의 시작 시각(UTC)", examples=["2026-11-02T03:10:00Z"]
    )
    finished_at: datetime | None = Field(
        description="가장 최근 실행의 종료 시각(UTC). 진행 중이면 null",
        examples=["2026-11-02T03:10:30Z"],
    )
    received_count: int | None = Field(
        description="가장 최근 실행이 받은 레코드 수. 진행 중·실패면 null 일 수 있음",
        examples=[73000],
    )
    loaded_count: int | None = Field(
        description="적재한 레코드 수 (= receivedCount − rejectedCount)",
        examples=[73000],
    )
    rejected_count: int | None = Field(
        description="거절한 레코드 수. 0 보다 크면 PARTIAL_SUCCESS", examples=[0]
    )
    open_issue_count: int = Field(
        description="이 원천의 수집에서 생긴 미해결(OPEN) 검증 이슈 수. 연동 데이터 탭의 오류 건수 배지",
        examples=[2],
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
        description=(
            "데이터가 마지막으로 반영된 시각(UTC). 마지막 성공 수집의 종료 시각으로, "
            "최근 실행이 실패해도 유지된다. 성공한 적이 없으면 null"
        ),
        examples=["2026-11-02T03:10:30Z"],
    )


class LatestDataSyncRunsResponse(CamelModel):
    """GET /api/v1/data-sync-runs/latest 응답"""

    items: list[DataSourceSyncStatusResponse] = Field(
        description=(
            "원천 유형별 마지막 수집 상태. 항상 ORDER·INVENTORY·SHIPMENT·PRODUCT·WAREHOUSE 순서로 다섯 항목"
        )
    )


class DataSyncRunCreateRequest(CamelModel):
    """
    수동 동기화 요청 (API 25 POST /data-sync-runs, 2026-11-09 결정)
      - 어느 카드(sourceSystem)를 눌러도 다섯 원천을 상품 → 창고 → 주문 → 선적 → 재고 순서로 함께 수집한다.
      - sourceSystem 과 sourceDomain 중 하나만 보낸다. sourceDomain 은 이전 요청 모양(하위 호환)이며 동작은 같다.
    """

    source_system: SourceSystem | None = Field(
        default=None,
        description="누른 카드의 원천 시스템(ERP·WMS·TMS). 기록용이며 어느 값이든 같은 전체 수집",
        examples=["ERP"],
    )
    source_domain: SourceDomain | None = Field(
        default=None,
        description="이전 요청 모양(하위 호환). sourceSystem 대신 보낼 수 있고 동작은 같다",
        examples=[None],
    )
    advance: SyncAdvance = Field(
        default=SyncAdvance.TICK,
        description=(
            "TICK: 가상 10분 한 번(기본). NEXT_DAY: 다음 자정 tick 까지 이어서 수집(데모의 '다음 날로', "
            "가상 시각일 때만, 아니면 422)"
        ),
        examples=["TICK"],
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> DataSyncRunCreateRequest:
        if (self.source_system is None) == (self.source_domain is None):
            raise ValueError("sourceSystem 과 sourceDomain 중 하나만 보내야 합니다")
        return self


class DataSyncRunStartedResponse(CamelModel):
    """수동 동기화가 시작한 원천 수집 하나 (202 응답 배열의 항목). 결과는 GET /data-sync-runs/latest 로 다시 조회한다."""

    source_domain: SourceDomain = Field(examples=["PRODUCT"])
    data_sync_run_id: uuid.UUID
    status: DataSyncRunStatus = Field(
        description="항상 RUNNING (백그라운드에서 적용)", examples=["RUNNING"]
    )
