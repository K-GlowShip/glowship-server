import uuid
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.locks import lock_analysis_run
from app.modules.delay_analysis.application.result_save.result_rows import (
    AnalysisResult,
    build_result_rows,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import AnalysisRunStatus
from app.modules.delay_analysis.infrastructure.at_risk_order import AtRiskOrder
from app.modules.delay_analysis.infrastructure.inventory_impact_result import (
    InventoryImpactResult,
)
from app.modules.delay_analysis.infrastructure.option_order_outcome import (
    OptionOrderOutcome,
)
from app.modules.delay_analysis.infrastructure.response_option import ResponseOption

"""
    분석 결과 저장 = 분석 완료 처리 (serviceflow 5.2 잠금 규약 "L2: 분석 완료 처리", agentflow 5.5 출력 표, GLOW-128)
      - 에이전트는 업무 DB 에 쓰지 않는다. 서비스가 받은 결과를 이 함수로 저장한다.
      - 실행 잠금(L2)을 잡고 최신 상태를 다시 읽는다. 아직 진행 중(DETECTED~VALIDATING_OUTPUT)일 때만
        결과 표 4개 INSERT 와 analysis_runs 갱신을 함께 한다. 그 사이 수집이 STALE 로 바꿨으면
        아무것도 쓰지 않는다(STALE 을 덮어쓰지 않고, 결과 행만 남는 반쪽 상태도 만들지 않음).
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
    saved: bool
    status: str  # 저장했으면 새 상태, 아니면 그때의 상태(예: STALE)


async def save_analysis_result(
    db: AsyncSession,
    *,
    company_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    result: AnalysisResult,
) -> ResultSaveOutcome:
    """정상 완료 결과를 저장한다. 실행이 이미 진행 중이 아니면(STALE 등) 아무것도 쓰지 않고 saved=False."""
    # 잠그기 전에 행을 만들어 둔다. 입력이 맞지 않으면(ResultSaveInputError) 잠금도 잡지 않는다.
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
    for column, value in rows.run_values.items():
        setattr(run, column, value)
    run.finished_at = func.clock_timestamp()
    await db.flush()
    return ResultSaveOutcome(saved=True, status=run.status)
