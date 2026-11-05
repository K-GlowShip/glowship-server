import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.security.tenant import TenantContext
from app.modules.decision.application.draft_policy import draft_rejection
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
    DecisionException,
)
from app.modules.decision.infrastructure.repository.member_draft_repository import (
    MemberDraftRepository,
    NewMemberDraft,
)
from app.modules.delay_analysis.application.exception.analysis_run_query_exceptions import (
    AnalysisRunNotFoundException,
)

"""
    의견·선택 초안 저장 서비스 (API 18 POST /analysis-runs/{runId}/drafts, API 명세 v6.2 4.6절)
      - 순서: 실행 조회(고객사에 없으면 404) → 초안 허용 판정(draft_policy) → 대응안 소속(OPTION_NOT_IN_RUN) → 한 행 추가.
      - 요청자 역할(CLIENT_MEMBER)과 고객사 범위·RLS 컨텍스트는 라우터 의존성이 먼저 정합니다.
      - 입력 형식(둘 중 하나 필수, 의견 앞뒤 공백 제거 뒤 1~600자, C25)은 요청 스키마가 먼저 거릅니다.
      - 거부한 시도는 행을 만들지 않습니다(4.6). 커밋은 요청 단위 세션(get_db)이 합니다.
      - created_at 은 서버의 지금 시각(UTC)입니다.
"""


@dataclass(frozen=True)
class SavedMemberDraft:
    member_draft_id: uuid.UUID
    created_at: datetime


class MemberDraftService:
    def __init__(
        self,
        repo: MemberDraftRepository,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.repo = repo
        self.now = now

    async def save_draft(
        self,
        tenant: TenantContext,
        *,
        analysis_run_id: uuid.UUID,
        response_option_id: uuid.UUID | None,
        opinion_text: str | None,
    ) -> SavedMemberDraft:
        target = await self.repo.find_draft_target(tenant.company_id, analysis_run_id)
        if target is None:
            raise AnalysisRunNotFoundException()
        rejection = draft_rejection(target)
        if rejection is not None:
            raise DecisionException(rejection)
        if response_option_id is not None and not await self.repo.option_in_run(
            tenant.company_id, analysis_run_id, response_option_id
        ):
            raise DecisionException(DecisionErrorCode.OPTION_NOT_IN_RUN)

        draft = NewMemberDraft(
            member_draft_id=uuid.uuid7(),
            analysis_run_id=analysis_run_id,
            user_id=tenant.user_id,
            response_option_id=response_option_id,
            opinion_text=opinion_text,
            created_at=self.now(),
        )
        await self.repo.insert_member_draft(tenant.company_id, draft)
        return SavedMemberDraft(
            member_draft_id=draft.member_draft_id, created_at=draft.created_at
        )
