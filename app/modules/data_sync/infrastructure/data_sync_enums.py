from enum import Enum


class SourceDomain(str, Enum):
    """외부 데이터 수집 도메인. data_sync_runs.source_domain CHECK 제약(data_sync_runs_ck1)과 같은 값이다."""

    PRODUCT = "PRODUCT"
    WAREHOUSE = "WAREHOUSE"
    ORDER = "ORDER"
    SHIPMENT = "SHIPMENT"
    INVENTORY = "INVENTORY"


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
