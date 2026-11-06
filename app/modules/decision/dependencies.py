from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)
from app.modules.decision.application.decision_clock import DecisionClock
from app.modules.decision.application.final_decision_service import (
    FinalDecisionService,
)
from app.modules.decision.application.member_draft_service import MemberDraftService
from app.modules.decision.application.reanalysis_service import ReanalysisService
from app.modules.decision.infrastructure.repository.final_decision_repository import (
    FinalDecisionRepository,
)
from app.modules.decision.infrastructure.repository.member_draft_repository import (
    MemberDraftRepository,
)

"""
    decision 모듈 의존성 조립
      - 요청 단위 DB 세션(get_db)을 받아 리포지토리와 서비스를 만들어 라우터에 주입합니다.
      - 같은 요청의 get_tenant_context 가 이 세션에 RLS 컨텍스트를 설정해 둡니다 (FastAPI 의존성 캐시).
"""


def get_decision_clock(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> DecisionClock:
    return DecisionClock(
        DataSyncRunRepository(db), virtual=settings.virtual_clock_start is not None
    )


def get_member_draft_service(
    db: AsyncSession = Depends(get_db),
    clock: DecisionClock = Depends(get_decision_clock),
) -> MemberDraftService:
    return MemberDraftService(repo=MemberDraftRepository(db), clock=clock)


def get_reanalysis_service(
    db: AsyncSession = Depends(get_db),
) -> ReanalysisService:
    return ReanalysisService(db=db, repo=MemberDraftRepository(db))


def get_final_decision_service(
    db: AsyncSession = Depends(get_db),
    clock: DecisionClock = Depends(get_decision_clock),
) -> FinalDecisionService:
    return FinalDecisionService(db=db, repo=FinalDecisionRepository(db), clock=clock)
