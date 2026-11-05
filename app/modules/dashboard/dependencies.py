from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.dashboard.application.dashboard_query_service import (
    DashboardQueryService,
)
from app.modules.dashboard.infrastructure.repository.dashboard_repository import (
    DashboardRepository,
)

"""
    dashboard 모듈 의존성 조립
      - 요청 단위 DB 세션(get_db)을 받아 리포지토리와 서비스를 만들어 라우터에 주입합니다.
      - 같은 요청의 get_tenant_context 가 이 세션에 RLS 컨텍스트를 설정해 둡니다 (FastAPI 의존성 캐시).
"""


def get_dashboard_repository(
    db: AsyncSession = Depends(get_db),
) -> DashboardRepository:
    return DashboardRepository(db)


def get_dashboard_query_service(
    repo: DashboardRepository = Depends(get_dashboard_repository),
) -> DashboardQueryService:
    return DashboardQueryService(repo=repo)
