import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.locks import lock_analysis_run
from app.core.rls_context import set_rls_context
from app.modules.delay_analysis.application.agent.retry import (
    UNEXPECTED_TOOL_ERROR,
    FailureSummary,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from app.modules.delay_analysis.application.result_save.result_save_service import (
    ResultSaveOutcome,
    save_analysis_failure,
)
from app.modules.delay_analysis.application.run_creation.run_creation_service import (
    record_input_data_versions,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import AnalysisRunStatus
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun

"""
    실행 상태 전이와 서버 시작 정리 조회 (GLOW-128, docs/analysis/auto-start.md 3절)
      - 모두 짧은 트랜잭션이다. 실행 잠금(L2)만 잡고 고객사 잠금(L1)은 잡지 않는다: 실행 행 하나만 바꾸고,
        수집 적용(L1 → L2)과 서로 기다리는 순환을 만들지 않기 위해서다(serviceflow 5.2 잠금 순서).
      - 고객사 행 보안 때문에 트랜잭션마다 고객사 문맥을 넣는다(set_config 는 트랜잭션 범위).
      - 커밋은 부르는 쪽(실행기)이 한다.

    시작 전이 begin_analysis (DETECTED → VALIDATING, 사용자 결정 11-08)
      - 확인하는 것은 상태 하나: DETECTED 일 때만 시작한다. STALE 로 바뀌었거나 이미 시작한 실행은 시작하지 않는다
        (같은 실행이 두 번 돌지 않게 하는 DB 쪽 방어선).
      - 에피소드 열림은 보지 않는다. 회복(DELAY_RECOVERED) 실행은 수집이 실행을 만든 뒤 같은 트랜잭션에서 에피소드를
        닫으므로(#63), 닫힌 에피소드에서 시작해 NO_DELAY 로 끝나야 한다(확정 DB E8 v2).
      - is_latest 가 거짓이면 경고 로그만 남기고 시작한다. 지금 생성 경로로는 생길 수 없는 상태이고, 건너뛰면
        영원히 DETECTED 로 남는다.
      - 입력 데이터 버전은 여기서 기록하지 않는다. 보류(ON_HOLD)가 아닌 종료를 저장할 때 기록한다
        (run_creation_service 규칙, 확정 DB 의 보류 실행만 데이터 버전 0건). outcome_save.save_graph_outcome·
        fail_unfinished 참고(사용자 결정 11-10, docs/analysis/auto-start.md 3절).

    단계 전이 advance_status (VALIDATING → ANALYZING_IMPACT → ANALYZING_OPTIONS → VALIDATING_OUTPUT)
      - 명세 8절 "진행 중일 때만 상태 전이(L2)". 진행 중이고 앞으로 가는 전이만 한다. 그 사이 STALE 이 됐으면
        아무것도 바꾸지 않는다(덮어쓰지 않음). 서버가 죽었을 때 정리가 "그때 단계"로 FAILED 를 남길 수 있게 한다.

    서버 시작 정리 조회 find_unfinished_runs
      - 고객사 문맥 없이 모든 고객사의 끝나지 않은 실행을 찾는다. 앱·워커 역할은 문맥 없이 행을 볼 수 없어
        DB 함수 app_worker_find_unfinished_runs(리비전 0009, SECURITY DEFINER)를 쓴다(사용자 결정 11-09).
"""

logger = logging.getLogger(__name__)

# 진행 중 상태의 앞뒤 순서(DETECTED 는 시작 전)
IN_PROGRESS_STEPS = (
    AnalysisStep.VALIDATING,
    AnalysisStep.ANALYZING_IMPACT,
    AnalysisStep.ANALYZING_OPTIONS,
    AnalysisStep.VALIDATING_OUTPUT,
)

# 서버가 다시 시작돼 분석 도중 멈춘 실행의 안내 문구(agentflow 7.1 failure_message)
RESTART_FAILURE_MESSAGE = (
    "서버가 다시 시작돼 분석이 중단되었습니다. 재분석을 요청하세요."
)

_FIND_UNFINISHED_SQL = text(
    "SELECT company_id, analysis_run_id, status FROM app_worker_find_unfinished_runs()"
)


@dataclass(frozen=True)
class StartedRun:
    """
    시작한 실행의 값(복사본). 시작 전이를 커밋한 뒤에도 세션 없이 읽는다(ORM 객체는 커밋 때 만료될 수 있음).
    엔진 입력(AnalysisRunLike)·저장·데이터 버전 기록이 읽는 칸을 모두 담는다.
    """

    analysis_run_id: uuid.UUID
    company_id: uuid.UUID
    delay_event_id: uuid.UUID
    operating_rule_set_id: uuid.UUID
    input_seq: int
    started_at: datetime
    trigger_shipment_schedule_snapshot_id: uuid.UUID | None
    is_latest: bool

    @classmethod
    def of(cls, run: AnalysisRun) -> StartedRun:
        return cls(
            analysis_run_id=run.analysis_run_id,
            company_id=run.company_id,
            delay_event_id=run.delay_event_id,
            operating_rule_set_id=run.operating_rule_set_id,
            input_seq=run.input_seq,
            started_at=run.started_at,
            trigger_shipment_schedule_snapshot_id=run.trigger_shipment_schedule_snapshot_id,
            is_latest=run.is_latest,
        )


@dataclass(frozen=True)
class UnfinishedRun:
    company_id: uuid.UUID
    analysis_run_id: uuid.UUID
    status: str


async def _lock_in_company(
    db: AsyncSession, company_id: uuid.UUID, analysis_run_id: uuid.UUID
) -> AnalysisRun | None:
    await set_rls_context(db, user_id=None, company_id=company_id)
    run = await lock_analysis_run(db, analysis_run_id)
    if run is None or run.company_id != company_id:
        logger.warning(
            "분석 실행을 찾지 못했습니다 (analysis_run_id=%s, company_id=%s)."
            " 워커 역할 세션·고객사가 맞는지, 실행을 만든 트랜잭션이 커밋됐는지 확인하세요",
            analysis_run_id,
            company_id,
        )
        return None
    return run


async def begin_analysis(
    db: AsyncSession, *, company_id: uuid.UUID, analysis_run_id: uuid.UUID
) -> StartedRun | None:
    """DETECTED 이면 VALIDATING 으로 바꾸고 그 실행의 값을, 아니면(이미 시작·STALE·없음) None 을 돌려준다."""
    run = await _lock_in_company(db, company_id, analysis_run_id)
    if run is None:
        return None
    if run.status != AnalysisRunStatus.DETECTED:
        logger.info(
            "분석 실행이 DETECTED 가 아니라 시작하지 않습니다 (analysis_run_id=%s, status=%s)",
            analysis_run_id,
            run.status,
        )
        return None
    if not run.is_latest:
        logger.warning(
            "현재 실행이 아닌 DETECTED 실행을 시작합니다 (analysis_run_id=%s). 결과는 이력에만 남습니다",
            analysis_run_id,
        )
    run.status = AnalysisRunStatus.VALIDATING
    await db.flush()
    return StartedRun.of(run)


async def advance_status(
    db: AsyncSession,
    *,
    company_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    to: AnalysisStep,
) -> bool:
    """진행 중이고 to 가 지금보다 뒤 단계일 때만 바꾸고 True. STALE·이미 끝남·같거나 앞 단계면 False."""
    run = await _lock_in_company(db, company_id, analysis_run_id)
    if run is None or run.status not in IN_PROGRESS_STEPS:
        return False
    if IN_PROGRESS_STEPS.index(to) <= IN_PROGRESS_STEPS.index(AnalysisStep(run.status)):
        return False
    run.status = to
    await db.flush()
    return True


async def find_unfinished_runs(db: AsyncSession) -> list[UnfinishedRun]:
    """모든 ACTIVE 고객사의 끝나지 않은 실행(DETECTED~VALIDATING_OUTPUT). 읽기만 한다."""
    rows = (await db.execute(_FIND_UNFINISHED_SQL)).all()
    return [UnfinishedRun(r.company_id, r.analysis_run_id, r.status) for r in rows]


def unexpected_failure(step: AnalysisStep, message: str) -> FailureSummary:
    """실행기가 끝내는 FAILED(분류되지 않은 오류, agentflow 7.1). 시도 1·소진 아님(그래프 노드의 같은 실패와 같은 값)."""
    return FailureSummary(
        status=AnalysisOutcome.FAILED,
        failed_step=step,
        failure_code=UNEXPECTED_TOOL_ERROR,
        attempt_count=1,
        retry_exhausted=False,
        failure_message=message,
    )


async def fail_unfinished(
    db: AsyncSession,
    *,
    company_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    message: str,
    now: datetime | None = None,
) -> ResultSaveOutcome | None:
    """
    진행 중(VALIDATING~VALIDATING_OUTPUT)인 실행을 지금 단계로 FAILED 저장한다. DETECTED·끝난 실행은 그대로(None).
    DETECTED 를 두는 이유: 아직 아무것도 하지 않은 실행이라 다음 시작 정리가 다시 시작한다.
    입력 데이터 버전도 같은 트랜잭션에서 기록한다. 보류가 아닌 종료는 모두 기록하는 규칙이라, 그래프 밖에서
    끝내는 FAILED(마지막 잡기·시작 정리)도 그래프 안 FAILED 와 같게 5건이 된다(확정 DB FAILED 실행과 같음).
    이미 있으면 다시 넣지 않는다(자동 실행은 만들 때 기록).
    now: 끝난 시각(저장 함수와 같은 규칙, 가상 시각이면 실행 시작 시각을 넘긴다).
    """
    await set_rls_context(db, user_id=None, company_id=company_id)
    run = await lock_analysis_run(db, analysis_run_id)
    if run is None or run.status not in IN_PROGRESS_STEPS:
        return None
    await record_input_data_versions(db, run)
    return await save_analysis_failure(
        db,
        company_id=company_id,
        analysis_run_id=analysis_run_id,
        summary=unexpected_failure(AnalysisStep(run.status), message),
        now=now,
    )
