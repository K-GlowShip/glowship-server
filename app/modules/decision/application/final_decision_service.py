import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.locks import lock_analysis_run
from app.core.security.tenant import TenantContext
from app.modules.decision.application.decision_clock import DecisionClock
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
    DecisionException,
)
from app.modules.decision.application.selection_policy import selection_rejection
from app.modules.decision.infrastructure.repository.final_decision_repository import (
    FinalDecisionRepository,
    NewFinalDecision,
    StoredFinalDecision,
)
from app.modules.delay_analysis.application.exception.analysis_run_query_exceptions import (
    AnalysisRunNotFoundException,
)
from app.modules.delay_analysis.application.stale.stale_service import mark_stale
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    AnalysisRunStatus,
    StaleReasonCode,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

"""
    최종 선택 서비스 (API 19 POST /analysis-runs/{runId}/final-decision, API 명세 v6.2 4.6절)
      - 순서: 같은 Idempotency-Key 확인 → 고객사에 없는 실행 404 → L2(그 실행만, 명세 "최종 선택은 L2만") → 잠근 뒤
        같은 키 다시 확인 → 재확인 2~7(selection_policy) → final_decisions 한 행 + 실행 FINALIZED.
      - 같은 키: 같은 실행이면 저장된 결과를 그대로 돌려주고(재요청), 다른 실행이면 409 IDEMPOTENCY_KEY_REUSED(11/05 결정).
        잠근 뒤 다시 보는 것은 같은 키·같은 실행 요청이 동시에 들어와 두 번째가 L2 를 기다린 경우 재요청으로 답하기 위해서입니다.
      - 시간 경과(TIME_ELAPSED)면 실행을 STALE(stale_detected_seq 없음)로 기록해 **먼저 커밋한 뒤** 409 ANALYSIS_STALE 로 거부합니다.
        요청 세션(get_db)은 예외에서 롤백하므로 여기서 커밋해야 STALE 기록이 남습니다.
      - checked_seq = 확인 시점 고객사의 최대 적용 순번. 실행의 완료 판정(completed_status READY_FOR_REVIEW)은 그대로 둡니다.
      - 동시에 다른 키로 같은 실행을 고르면 두 번째는 L2 뒤 재확인에서 FINAL_DECISION_EXISTS 입니다. DB 제약(uk2·uk3)에 걸리면
        같은 코드로 바꿉니다.
      - 판단 시각(선택일·decided_at)은 decision_clock 입니다. 가상 시각이면 고객사의 가장 최근 수집 시작 시각이라
        7월 데이터의 약속일과 같은 시간축에서 시간을 확인합니다.
"""


@dataclass(frozen=True)
class FinalDecisionResult:
    final_decision_id: uuid.UUID
    decided_at: datetime
    checked_seq: int
    status: str
    replayed: bool  # 같은 Idempotency-Key 재요청으로 저장된 결과를 돌려줌


def _result(stored: StoredFinalDecision, *, replayed: bool) -> FinalDecisionResult:
    return FinalDecisionResult(
        final_decision_id=stored.final_decision_id,
        decided_at=stored.decided_at,
        checked_seq=stored.checked_seq,
        status=AnalysisRunStatus.FINALIZED,
        replayed=replayed,
    )


class FinalDecisionService:
    def __init__(
        self,
        db: AsyncSession,
        repo: FinalDecisionRepository,
        clock: DecisionClock | None = None,
    ):
        self.db = db
        self.repo = repo
        self.clock = clock or DecisionClock(None, virtual=False)

    async def _replay(
        self, company_id: uuid.UUID, analysis_run_id: uuid.UUID, idempotency_key: str
    ) -> FinalDecisionResult | None:
        stored = await self.repo.find_by_idempotency_key(company_id, idempotency_key)
        if stored is None:
            return None
        if stored.analysis_run_id != analysis_run_id:
            raise DecisionException(DecisionErrorCode.IDEMPOTENCY_KEY_REUSED)
        return _result(stored, replayed=True)

    async def decide(
        self,
        tenant: TenantContext,
        *,
        analysis_run_id: uuid.UUID,
        response_option_id: uuid.UUID,
        reason_text: str,
        idempotency_key: str,
    ) -> FinalDecisionResult:
        company_id = tenant.company_id
        replay = await self._replay(company_id, analysis_run_id, idempotency_key)
        if replay is not None:
            return replay

        decided_at = await self.clock.now(company_id)
        day = decided_at.astimezone(UTC).date()
        if (
            await self.repo.find_selection_target(
                company_id, analysis_run_id, response_option_id, day
            )
            is None
        ):
            raise AnalysisRunNotFoundException()
        run = await lock_analysis_run(self.db, analysis_run_id)
        replay = await self._replay(company_id, analysis_run_id, idempotency_key)
        if replay is not None:
            return replay
        target = await self.repo.find_selection_target(
            company_id, analysis_run_id, response_option_id, day
        )

        rejection = selection_rejection(target)
        if rejection is not None:
            if rejection.time_elapsed:
                mark_stale(run, [StaleReasonCode.TIME_ELAPSED], None, decided_at)
                await self.db.commit()
            raise DecisionException(rejection.code)

        checked_seq = await StaleChangeRepository(self.db).find_max_applied_seq(
            company_id
        )
        decision = NewFinalDecision(
            final_decision_id=uuid.uuid7(),
            analysis_run_id=analysis_run_id,
            user_id=tenant.user_id,
            response_option_id=response_option_id,
            reason_text=reason_text,
            idempotency_key=idempotency_key,
            decided_at=decided_at,
            checked_seq=checked_seq,
        )
        try:
            await self.repo.insert_final_decision(company_id, decision)
        except IntegrityError as error:
            constraint = str(error.orig)
            if "final_decisions_uk3" in constraint:
                raise DecisionException(
                    DecisionErrorCode.IDEMPOTENCY_KEY_REUSED
                ) from error
            if "final_decisions_uk2" in constraint:
                raise DecisionException(
                    DecisionErrorCode.FINAL_DECISION_EXISTS
                ) from error
            raise
        run.status = AnalysisRunStatus.FINALIZED
        await self.db.flush()
        return FinalDecisionResult(
            final_decision_id=decision.final_decision_id,
            decided_at=decided_at,
            checked_seq=checked_seq,
            status=AnalysisRunStatus.FINALIZED,
            replayed=False,
        )
