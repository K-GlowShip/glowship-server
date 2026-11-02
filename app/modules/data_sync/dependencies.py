from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.modules.data_sync.application.data_sync_status_service import (
    DataSyncStatusService,
)
from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)

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
        repo=repo, stale_multiplier=settings.sync_stale_multiplier
    )
