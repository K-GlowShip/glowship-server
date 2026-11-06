import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.locks import lock_analysis_run, lock_company
from app.core.security.tenant import TenantContext
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionException,
)
from app.modules.decision.application.reanalysis_policy import reanalysis_rejection
from app.modules.decision.infrastructure.repository.member_draft_repository import (
    MemberDraftRepository,
)
from app.modules.delay_analysis.application.exception.analysis_run_query_exceptions import (
    AnalysisRunNotFoundException,
)
from app.modules.delay_analysis.application.run_creation.run_creation_service import (
    create_analysis_run,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import TriggerReason

"""
    재분석 요청 서비스 (API 20 POST /analysis-runs/{runId}/reanalysis, API 명세 v6.2 4.6절·D10)
      - 잠금은 L1(고객사) → L2(그 실행) 순서입니다(serviceflow 5.2 "재분석"). L1 을 잡으면 수집(에피소드 종료·STALE 판정)과
        다른 재분석이 끝나기를 기다리고, L2 를 잡은 뒤 실행 상태를 다시 읽어 판정합니다.
      - 판정(reanalysis_policy) 을 통과하면 create_analysis_run(USER_REANALYSIS) 으로 새 실행을 만듭니다(개발자 3 함수).
        이전 실행은 is_latest 만 내리고 상태는 그대로입니다(재분석은 STALE 사유가 아님, 명세 4.6).
      - 새 실행은 DETECTED 이고, 분석을 다시 돌리는 일(입력 확정 이후)은 이 서비스 밖입니다(GLOW-128).
      - 고객사에 없는 실행은 잠그기 전에 404 로 거릅니다(로컬 superuser 는 RLS 를 우회하므로 잠금 전에 고객사 조건으로 확인).
      - 커밋은 요청 단위 세션(get_db)이 합니다. 거부하면 아무것도 바꾸지 않습니다.
"""


@dataclass(frozen=True)
class ReanalysisResult:
    run_id: uuid.UUID
    version_no: int
    parent_run_id: uuid.UUID
    status: str


class ReanalysisService:
    def __init__(self, db: AsyncSession, repo: MemberDraftRepository):
        self.db = db
        self.repo = repo

    async def request(
        self,
        tenant: TenantContext,
        *,
        analysis_run_id: uuid.UUID,
        request_reason: str | None,
    ) -> ReanalysisResult:
        company_id = tenant.company_id
        await lock_company(self.db, company_id)
        if await self.repo.find_draft_target(company_id, analysis_run_id) is None:
            raise AnalysisRunNotFoundException()
        run = await lock_analysis_run(self.db, analysis_run_id)
        # L2 를 기다리는 동안 바뀌었을 수 있어 잠근 뒤 다시 읽습니다
        target = await self.repo.find_draft_target(company_id, analysis_run_id)
        rejection = reanalysis_rejection(target)
        if rejection is not None:
            raise DecisionException(rejection)

        creation = await create_analysis_run(
            self.db,
            company_id=company_id,
            delay_event_id=run.delay_event_id,
            trigger_reason=TriggerReason.USER_REANALYSIS,
            requested_by_user_id=tenant.user_id,
            request_reason=request_reason,
        )
        new_run = creation.run
        return ReanalysisResult(
            run_id=new_run.analysis_run_id,
            version_no=new_run.version_no,
            parent_run_id=new_run.parent_analysis_run_id,
            status=new_run.status,
        )
