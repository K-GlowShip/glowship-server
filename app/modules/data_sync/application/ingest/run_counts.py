from dataclasses import dataclass

from app.modules.data_sync.infrastructure.data_sync_enums import DataSyncRunStatus

"""
    수집 실행 건수와 완료 상태 (data_sync_runs ck4~ck7)
      - 받은 건수 = 적재 건수 + 거부 건수 (ck5)
      - 거부가 없으면 SUCCEEDED (ck6), 하나라도 있으면 PARTIAL_SUCCESS (ck7)
      - 처리 중 예외로 아무것도 적용하지 못한 수집은 FAILED 이고 건수·적용 순번을 남기지 않습니다 (ck8).
"""


@dataclass(frozen=True)
class RunCounts:
    received: int
    loaded: int
    rejected: int

    def __post_init__(self):
        if min(self.received, self.loaded, self.rejected) < 0:
            raise ValueError("건수는 0 이상이어야 합니다")
        if self.received != self.loaded + self.rejected:
            raise ValueError(
                "받은 건수 = 적재 건수 + 거부 건수 이어야 합니다 (data_sync_runs_ck5)"
            )

    @property
    def status(self) -> DataSyncRunStatus:
        if self.rejected == 0:
            return DataSyncRunStatus.SUCCEEDED
        return DataSyncRunStatus.PARTIAL_SUCCESS
