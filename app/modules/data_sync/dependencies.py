from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db, open_worker_session
from app.modules.data_sync.application.data_sync_status_service import (
    DataSyncStatusService,
)
from app.modules.data_sync.application.manual_sync_service import ManualSyncService
from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)
from app.modules.data_sync.infrastructure.virtual_tms import VirtualTmsScenario

"""
    data_sync 모듈 의존성 조립
      - 요청 단위 DB 세션(get_db)을 받아 리포지토리와 서비스를 만들어 라우터에 주입합니다.
      - 같은 요청의 get_tenant_context 가 이 세션에 RLS 컨텍스트를 설정해 둡니다 (FastAPI 의존성 캐시).
"""


def get_data_sync_run_repository(
    db: AsyncSession = Depends(get_db),
) -> DataSyncRunRepository:
    return DataSyncRunRepository(db)


def get_data_sync_status_service(
    repo: DataSyncRunRepository = Depends(get_data_sync_run_repository),
    settings: Settings = Depends(get_settings),
) -> DataSyncStatusService:
    return DataSyncStatusService(
        repo=repo,
        stale_multiplier=settings.sync_stale_multiplier,
        virtual_clock=settings.virtual_clock_start is not None,
    )


def get_manual_sync_service(
    settings: Settings = Depends(get_settings),
) -> ManualSyncService:
    """수동 수집(API 25). 워커 전용 세션을 스스로 엽니다(요청 세션 get_db 를 쓰지 않음)."""
    path = settings.virtual_tms_scenario_path

    def load_scenario() -> VirtualTmsScenario | None:
        return VirtualTmsScenario.from_file(path) if path is not None else None

    return ManualSyncService(
        open_session=open_worker_session,
        virtual_start=settings.virtual_clock_start,
        load_scenario=load_scenario,
    )
