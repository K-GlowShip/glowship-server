import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.locks import lock_analysis_run
from app.modules.delay_analysis.application.result_save.result_rows import (
    AnalysisResult,
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
      - 범위: 정상 완료(READY_FOR_REVIEW·NO_RISK). 실패·보류 저장은 실패 요약(GLOW-123) 쪽에서 한다.
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
    run = await lock_analysis_run(db, analysis_run_id)
    if run is None or run.company_id != company_id:
        raise AnalysisRunNotFoundError(f"분석 실행이 없습니다: {analysis_run_id}")
    if run.status not in IN_PROGRESS:
        return ResultSaveOutcome(saved=False, status=run.status)

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

    # Q-13a: 확정된 의존 범위로 입력 순번 이후 변경을 다시 판정 (같은 L2·같은 트랜잭션)
    staled = await StaleJudgmentService(
        db, StaleChangeRepository(db), recalculator
    ).recheck_run(analysis_run_id, detected_at=now)
    return ResultSaveOutcome(
        saved=True, status=AnalysisRunStatus.STALE if staled else run.status
    )
