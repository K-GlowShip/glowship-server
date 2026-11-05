import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.locks import lock_analysis_run
from app.modules.delay_analysis.application.agent.retry import (
    EXPLANATION_GENERATION_FAILED,
    FailureSummary,
)
from app.modules.delay_analysis.application.result_save.result_rows import (
    AnalysisResult,
    ResultRows,
    ResultSaveInputError,
    build_result_rows,
)
from app.modules.delay_analysis.application.stale.stale_service import (
    SignatureRecalculator,
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    AnalysisRunStatus,
    CompletedStatus,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.at_risk_order import AtRiskOrder
from app.modules.delay_analysis.infrastructure.inventory_impact_result import (
    InventoryImpactResult,
)
from app.modules.delay_analysis.infrastructure.option_order_outcome import (
    OptionOrderOutcome,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)
from app.modules.delay_analysis.infrastructure.response_option import ResponseOption

"""
    분석 결과 저장 = 분석 완료 처리 (serviceflow 5.2 처리 단계 5·잠금 규약 "L2: 분석 완료 처리", agentflow 5.5 출력 표, GLOW-128)
      - 에이전트는 업무 DB 에 쓰지 않는다. 서비스가 받은 결과를 이 함수로 저장한다.
      - 실행 잠금(L2)을 잡고 최신 상태를 다시 읽는다. 아직 진행 중(DETECTED~VALIDATING_OUTPUT)일 때만
        결과 표 4개 INSERT 와 analysis_runs 갱신을 함께 한다. 그 사이 수집이 STALE 로 바꿨으면
        아무것도 쓰지 않는다(STALE 을 덮어쓰지 않고, 결과 행만 남는 반쪽 상태도 만들지 않음).
      - 저장한 뒤 같은 트랜잭션에서, 확정된 의존 범위로 입력 순번 이후 변경 전체를 다시 판정한다
        (Q-13a, StaleJudgmentService.recheck_run). 분석하는 동안 진행 중 실행은 의존 범위가 없어
        지연 선적 SKU 로만 판정되었고, 수집 쪽 판정은 그 수집의 변경만 보기 때문이다. 걸리면 저장한 결과는
        그대로 두고 STALE 로 기록한다(serviceflow 5.2 "이미 저장한 결과는 보존").
      - 커밋은 호출하는 쪽이 한다. 에이전트 분석은 이 트랜잭션 밖에서 끝나 있어야 한다(긴 작업은 잠그지 않음).
      - save_analysis_result: 정상 완료(READY_FOR_REVIEW·NO_RISK). 저장 뒤 재판정한다.
      - save_analysis_failure: 실패·보류(FAILED·ON_HOLD). 값은 실패 요약(FailureSummary, agent/retry.py)이
        DDL 규칙(ck10~14·ck23·ck26·ck36)대로 만든다. 입력 검증 보류도 이 함수로 저장한다(판정은 입력 검증,
        사용자 결정 11-05). FAILED·ON_HOLD 는 STALE 대상이 아니라 재판정하지 않는다(stale_service STALE_TARGET_STATUSES).
"""

IN_PROGRESS = frozenset(
    {
        AnalysisRunStatus.DETECTED,
        AnalysisRunStatus.VALIDATING,
        AnalysisRunStatus.ANALYZING_IMPACT,
        AnalysisRunStatus.ANALYZING_OPTIONS,
        AnalysisRunStatus.VALIDATING_OUTPUT,
    }
)


class AnalysisRunNotFoundError(LookupError):
    """저장할 실행이 없거나 다른 고객사의 실행"""


@dataclass(frozen=True)
class ResultSaveOutcome:
    saved: bool  # 결과를 저장했는가 (진행 중이 아니었으면 False, 아무것도 쓰지 않음)
    status: str  # 지금 상태: 저장했으면 완료 상태 또는 재판정으로 바뀐 STALE, 아니면 그때의 상태


def _check_explanation(result: AnalysisResult) -> None:
    """READY_FOR_REVIEW 는 검증을 통과한 설명이 있어야 하고, NO_RISK 는 설명이 없다 (엔진 하네스는 READY 만 설명을 만든다).
    설명 없이 READY 로 저장하면 최종 선택 재확인(FAILED 만 거름)을 통과해 설명 없는 실행이 선택될 수 있다."""
    ready = result.inventory_impact.completed_status == CompletedStatus.READY_FOR_REVIEW
    if ready and result.explanation is None:
        raise ResultSaveInputError(
            "READY_FOR_REVIEW 는 검증을 통과한 설명이 있어야 합니다. 설명이 계속 실패하면 save_analysis_failure 로 저장합니다"
        )
    if not ready and result.explanation is not None:
        raise ResultSaveInputError("NO_RISK 에는 설명이 없습니다")


async def _lock_in_progress(
    db: AsyncSession, company_id: uuid.UUID, analysis_run_id: uuid.UUID
) -> AnalysisRun | ResultSaveOutcome:
    """실행 잠금(L2)을 잡고 최신 상태를 읽는다. 진행 중이 아니면 쓰지 않을 결과(saved=False)를 돌려준다."""
    run = await lock_analysis_run(db, analysis_run_id)
    if run is None or run.company_id != company_id:
        raise AnalysisRunNotFoundError(f"분석 실행이 없습니다: {analysis_run_id}")
    if run.status not in IN_PROGRESS:
        return ResultSaveOutcome(saved=False, status=run.status)
    return run


async def _write(db: AsyncSession, run: AnalysisRun, rows: ResultRows) -> datetime:
    """결과 행과 실행 값을 넣고, 끝난 시각(DB 시각)을 돌려준다."""
    # 위험 주문 → 대응안 → 주문 결과 순서로 넣는다 (주문 결과가 앞의 두 표를 참조)
    db.add_all(InventoryImpactResult(**r) for r in rows.inventory_impact_results)
    db.add_all(AtRiskOrder(**r) for r in rows.at_risk_orders)
    db.add_all(ResponseOption(**r) for r in rows.response_options)
    await db.flush()
    db.add_all(OptionOrderOutcome(**r) for r in rows.option_order_outcomes)
    now = await db.scalar(select(func.clock_timestamp()))
    for column, value in rows.run_values.items():
        setattr(run, column, value)
    run.finished_at = now
    await db.flush()
    return now


async def save_analysis_result(
    db: AsyncSession,
    *,
    company_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    result: AnalysisResult,
    recalculator: SignatureRecalculator | None = None,
) -> ResultSaveOutcome:
    """
    정상 완료 결과를 저장하고 바로 다시 판정한다. 실행이 이미 진행 중이 아니면(STALE 등) 아무것도 쓰지 않고 saved=False.
    recalculator 는 재판정 2차(판정 서명 재계산)에 쓴다. 없으면 의존 범위에 걸린 변경이 있으면 STALE(놓치지 않는 쪽).
    """
    # 잠그기 전에 행을 만들어 둔다. 입력이 맞지 않으면(ResultSaveInputError) 잠금도 잡지 않는다.
    _check_explanation(result)
    rows = build_result_rows(
        result, company_id=company_id, analysis_run_id=analysis_run_id
    )
    run = await _lock_in_progress(db, company_id, analysis_run_id)
    if isinstance(run, ResultSaveOutcome):
        return run
    now = await _write(db, run, rows)

    # Q-13a: 확정된 의존 범위로 입력 순번 이후 변경을 다시 판정 (같은 L2·같은 트랜잭션)
    staled = await StaleJudgmentService(
        db, StaleChangeRepository(db), recalculator
    ).recheck_run(analysis_run_id, detected_at=now)
    return ResultSaveOutcome(
        saved=True, status=AnalysisRunStatus.STALE if staled else run.status
    )


@dataclass(frozen=True)
class ExplanationModel:
    """설명을 만들려 한 모델·프롬프트 버전 (설명 실패여도 엔진이 남긴다)"""

    model_name: str
    prompt_version: str


async def save_analysis_failure(
    db: AsyncSession,
    *,
    company_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    summary: FailureSummary,
    computed: AnalysisResult | None = None,
    explanation_model: ExplanationModel | None = None,
) -> ResultSaveOutcome:
    """
    실패·보류를 저장한다 (agentflow 5.5 "필수 자료 부족·검증 오류"·"도구 실패"·"설명 검증 계속 실패" 행).
      - 도구 실패·보류: 결과 행 없이 analysis_runs 의 상태와 실패 요약 5컬럼만. computed 는 넣지 않는다.
      - 설명 실패(EXPLANATION_GENERATION_FAILED): 계산 결과(computed)의 결과 표 행과 판정 서명은 저장하고,
        의존 범위·결과 분류는 비운다(ck36·ck21). 설명을 만들려 한 모델·프롬프트 버전도 남긴다(엔진 하네스와 같음).
      - 실행이 이미 진행 중이 아니면(STALE 등) 아무것도 쓰지 않고 saved=False.
    """
    columns: dict[str, Any] = summary.columns()
    if summary.failure_code == EXPLANATION_GENERATION_FAILED:
        if computed is None or explanation_model is None:
            raise ResultSaveInputError(
                "설명 실패는 계산 결과와 모델·프롬프트 버전이 있어야 합니다 (agentflow 5.5)"
            )
        if (
            computed.inventory_impact.completed_status
            != CompletedStatus.READY_FOR_REVIEW
            or computed.explanation is not None
        ):
            # 엔진은 READY_FOR_REVIEW 실행만 설명을 만든다(run_s1_s5.py analyze_run). 설명이 붙어 있으면 실패가 아니다
            raise ResultSaveInputError(
                "설명 실패의 계산 결과는 READY_FOR_REVIEW 이고 설명이 없어야 합니다"
            )
        rows = build_result_rows(
            computed, company_id=company_id, analysis_run_id=analysis_run_id
        )
        # 정상 완료 값(상태·결과 분류·의존 범위)은 쓰지 않는다
        rows.run_values = columns | {
            "decision_signature": computed.decision_signature,
            "decision_signature_sha256": computed.decision_signature_sha256,
            "model_name": explanation_model.model_name,
            "prompt_version": explanation_model.prompt_version,
        }
    else:
        if computed is not None or explanation_model is not None:
            raise ResultSaveInputError(
                f"{summary.failure_code} 는 결과 행을 저장하지 않습니다 (agentflow 5.5)"
            )
        rows = ResultRows(run_values=columns)
    run = await _lock_in_progress(db, company_id, analysis_run_id)
    if isinstance(run, ResultSaveOutcome):
        return run
    await _write(db, run, rows)
    return ResultSaveOutcome(saved=True, status=run.status)
