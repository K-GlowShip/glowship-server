from enum import Enum


class SourceSystem(str, Enum):
    """수동 동기화 요청의 원천 시스템(연동 화면의 카드). 어느 값이든 같은 전체 수집이다 (2026-11-09 결정)."""

    ERP = "ERP"
    WMS = "WMS"
    TMS = "TMS"


class SyncAdvance(str, Enum):
    """수동 동기화 한 번에 가상 시각을 얼마나 진행할지 (2026-11-09 결정)"""

    TICK = "TICK"  # 가상 10분 (tick 1번)
    NEXT_DAY = (
        "NEXT_DAY"  # 다음 자정 tick 까지 이어서 (데모의 "다음 날로", 가상 시각일 때만)
    )


class SourceDomain(str, Enum):
    """외부 데이터 수집 도메인. data_sync_runs.source_domain CHECK 제약(data_sync_runs_ck1)과 같은 값이다."""

    PRODUCT = "PRODUCT"
    WAREHOUSE = "WAREHOUSE"
    ORDER = "ORDER"
    SHIPMENT = "SHIPMENT"
    INVENTORY = "INVENTORY"


class IssueEntityType(str, Enum):
    """
    검증 이슈 data_validation_issues.source_entity_type (명세 v6.2 4.3 C21).
    DDL 의 엔터티 이름(data_change_records_ck1·external_entity_mappings_ck2)과 같습니다. 상세 줄 오류도 부모 이름이고
    위치는 field_name 에 둡니다. 수집 원천 유형(domain)과는 상품만 다릅니다 (PRODUCT → SKU).
    """

    SKU = "SKU"
    WAREHOUSE = "WAREHOUSE"
    ORDER = "ORDER"
    SHIPMENT = "SHIPMENT"
    INVENTORY = "INVENTORY"


ISSUE_ENTITY_TYPE_OF = {
    SourceDomain.PRODUCT: IssueEntityType.SKU,
    SourceDomain.WAREHOUSE: IssueEntityType.WAREHOUSE,
    SourceDomain.ORDER: IssueEntityType.ORDER,
    SourceDomain.SHIPMENT: IssueEntityType.SHIPMENT,
    SourceDomain.INVENTORY: IssueEntityType.INVENTORY,
}


class ConnectionMethod(str, Enum):
    """수집 연결 방식. data_sync_runs.connection_method CHECK 제약(data_sync_runs_ck2)과 같은 값이다."""

    API = "API"
    FILE = "FILE"


class DataSyncRunStatus(str, Enum):
    """수집 실행 상태. data_sync_runs.status CHECK 제약(data_sync_runs_ck3)과 같은 값이다."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"


# 데이터가 적재된 실행 상태. applied_seq 가 있는 실행과 같다 (data_sync_runs_ck8).
APPLIED_STATUSES = frozenset(
    {DataSyncRunStatus.SUCCEEDED, DataSyncRunStatus.PARTIAL_SUCCESS}
)
