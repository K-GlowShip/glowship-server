from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.decision.application.decision_list_service import (
    DecisionListService,
)
from app.modules.decision.infrastructure.repository.decision_repository import (
    DecisionRepository,
)

"""
    decision 모듈 의존성 조립
      - 요청 단위 DB 세션(get_db)을 받아 리포지토리와 서비스를 만들어 라우터에 주입합니다.
      - 같은 요청의 get_tenant_context 가 이 세션에 RLS 컨텍스트를 설정해 둡니다 (FastAPI 의존성 캐시).
"""


def get_decision_repository(db: AsyncSession = Depends(get_db)) -> DecisionRepository:
    return DecisionRepository(db)


def get_decision_list_service(
    repo: DecisionRepository = Depends(get_decision_repository),
) -> DecisionListService:
    return DecisionListService(repo=repo)
