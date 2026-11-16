import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.locks import lock_analysis_run
from app.modules.delay_analysis.application.agent.retry import (
    EXPLANATION_GENERATION_FAILED,
)
from app.modules.delay_analysis.application.agent.state import AnalysisOutcome
from app.modules.delay_analysis.application.result_save.result_rows import (
    AnalysisResult,
    Explanation,
    run_basis_from_tools,
)
from app.modules.delay_analysis.application.result_save.result_save_service import (
    ExplanationModel,
    ResultSaveOutcome,
    save_analysis_failure,
    save_analysis_result,
    save_no_delay,
)
from app.modules.delay_analysis.application.run_creation.run_creation_service import (
    record_input_data_versions,
)
from app.modules.delay_analysis.application.run_start.transitions import StartedRun
from app.modules.delay_analysis.application.stale.stale_service import (
    StaleJudgmentService,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    AnalysisRunStatus,
    CompletedStatus,
    StaleReasonCode,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.engine.signature_recalculator import (
    recalculator_or_none,
)
from app.modules.delay_analysis.infrastructure.repository.run_creation_repository import (
    RunCreationRepository,
)
from app.modules.delay_analysis.infrastructure.repository.stale_change_repository import (
    StaleChangeRepository,
)

"""
    그래프 종료 상태 → 저장 함수 (docs/agent-flow/agent-tool-interface.md 7절 표, agentflow 5.5 출력 표)
      | 종료                  | 저장 함수               | 넘기는 것
      | READY_FOR_REVIEW·NO_RISK | save_analysis_result | T6·T7·T10·T11 출력, run_basis_from_tools, 판정(decision_of), READY 면 설명
      | 설명 3회 실패(FAILED)  | save_analysis_failure  | 실패 요약, 설명 없는 계산 결과, 모델·프롬프트 판
      | 도구 실패·보류·도구 밖 예외 | save_analysis_failure | 실패 요약
      | NO_DELAY              | save_no_delay           | judge_no_delay 결과의 판정 서명·SHA-256
      | STALE(노드 경계)       | 저장 안 함              | 상태는 수집이 이미 STALE 로 바꿈
      - 종료 상태가 없으면(그래프가 outcome 없이 끝남) 연결 실수라 OutcomeMissingError 로 올린다. 실행기가 FAILED 로 남긴다.
      - 입력 데이터 버전(analysis_run_data_versions)은 보류(ON_HOLD)가 아니면 저장과 같은 트랜잭션에서 기록한다
        (STALE 로 멈춘 실행 포함). 입력 검증을 통과한 실행에만 기록하는 규칙(run_creation_service 머리말, 확정 DB 보류 실행
        0건·나머지 도메인 5건, 사용자 결정 11-10)이다. 그래프 밖에서 끝내는 FAILED 는 transitions.fail_unfinished 가 같은 규칙으로 기록한다.
        재분석·자동 실행 모두 만들 때 기록하지 않아 여기서 채워진다. 이미 있으면 다시 넣지 않는다.
      - 저장 함수들은 실행이 아직 진행 중일 때만 쓰고, 커밋은 부르는 쪽이 한다.
      - 운영 기준 버전(GLOW-160): 실행 행의 operating_rule_set_id 는 실행을 만들 때 실행 날짜로 고르고(run_creation_service),
        엔진은 분석을 시작할 때 기준일 D 로 따로 고른다(snapshot.build, T8 출력). 그 사이 새 버전이 저장되거나 D 가 실행 날짜보다
        앞서면(재고·주문 자료가 늦음, 이슈 #51) 둘이 다를 수 있다. 결과를 저장했고 T8 출력이 있으면 같은 트랜잭션에서 행을
        엔진이 실제로 쓴 버전으로 맞춘다. 그래야 분석 상세의 운영 기준 버전이 계산에 쓴 버전이고, 최종 선택 재확인
        (selection_policy, 행의 버전과 활성 버전 비교)이 실제 계산 버전으로 판정한다.
        보류(ON_HOLD)·지연 해소(NO_DELAY)는 T8 을 부르지 않아 행을 그대로 둔다.
      - 맞춘 버전이 저장하는 날의 활성 버전이 아니면(PR #160 리뷰): 맞춘 뒤 선택 대기(READY_FOR_REVIEW)로 저장된 실행의
        운영 기준이 끝난 시각 날짜의 활성 버전(최종 선택 재확인과 같은 식: effective_from ≤ 그날 중 version_no 최대)과 다르면,
        결과는 둔 채 같은 트랜잭션에서 STALE(OPERATING_RULE_CHANGED)로 남긴다(BR-012). 분석 중에 "오늘부터 적용" 새 버전이
        저장됐거나(운영 기준 저장은 선택 대기 실행만 STALE 로 바꿔 분석 중인 실행은 빠진다, GLOW-159), 기준일 D 가 당겨져
        이전 버전으로 계산한 경우다. 감지 순번은 고객사의 현재 최대 적용 순번(GLOW-159 와 같음), 감지 시각은 끝난 시각이다.
        위험 없음(NO_RISK)·실패(FAILED)는 바꾸지 않는다(GLOW-159 와 같은 범위). 고객사 잠금(L1)은 잡지 않아, 운영 기준
        저장 트랜잭션과 이 저장이 겹치는 짧은 순간은 양쪽 모두 놓칠 수 있고 그때는 최종 선택 재확인이 막는다.
"""

logger = logging.getLogger(__name__)

# 실행 행의 운영 기준을 엔진이 쓴 버전으로 고쳤을 때 남기는 로그 표시. 뒤에 실행 ID·이전 ID·엔진 ID·엔진 버전 번호
OPERATING_RULE_ADJUSTED_LOG_MARK = "OPERATING_RULE_ADJUSTED_TO_ENGINE"
# 계산에 쓴 운영 기준이 저장하는 날의 활성 버전이 아니라 STALE 로 남겼을 때의 로그 표시. 뒤에 실행 ID·계산 ID·활성 ID
OPERATING_RULE_NOT_ACTIVE_LOG_MARK = "OPERATING_RULE_NOT_ACTIVE_ON_SAVE"


class OutcomeMissingError(RuntimeError):
    """그래프가 종료 상태(outcome) 없이 끝났다. 그래프 연결 버그다."""


def analysis_result_from_state(
    state: Mapping[str, Any], *, with_explanation: bool = True
) -> AnalysisResult:
    """
    정상 완료(또는 설명 실패의 계산 결과) 저장 입력. 대응안·추천·설명은 READY_FOR_REVIEW 에만 있다.
    with_explanation=False 는 설명 실패 저장용(계산 결과만, 설명 없음).
    """
    impact = state["inventory_impact"]
    decision = state["decision"]
    ready = impact.completed_status == CompletedStatus.READY_FOR_REVIEW
    passed = state.get("explanation") if (ready and with_explanation) else None
    return AnalysisResult(
        basis=run_basis_from_tools(
            state["delay_context"],
            state["inventory_snapshot"],
            state["sales_history"],
            state["open_orders"],
            state["operating_rules"],
            [item.sku_code for item in impact.items],
        ),
        inventory_impact=impact,
        at_risk_orders=state["at_risk_orders"],
        option_outcomes=state.get("option_outcomes") if ready else None,
        option_ranking=state.get("option_ranking") if ready else None,
        dependency_scope=decision.dependency_scope,
        decision_signature=decision.decision_signature,
        decision_signature_sha256=decision.decision_signature_sha256,
        explanation=(
            Explanation(
                model_name=passed.model_name,
                prompt_version=passed.prompt_version,
                final_explanation_json=passed.final_explanation_json,
                generated_at=passed.generated_at,
            )
            if passed is not None
            else None
        ),
    )


async def save_graph_outcome(
    db: AsyncSession,
    run: StartedRun,
    state: Mapping[str, Any],
    *,
    now: datetime | None = None,
    recalculate: bool = True,
) -> ResultSaveOutcome | None:
    """
    그래프 종료 상태대로 저장한다. STALE 이면 저장하지 않고 None.
    recalculate=False: 저장 직후 재판정에 판정 서명 재계산을 쓰지 않는다(걸린 변경이 있으면 STALE, 놓치지 않는 쪽).
    실행기가 저장 시간 제한을 넘겨 다시 저장할 때만 넘긴다(이슈 #123).
    결과를 저장했고 상태에 데이터 경고가 있으면 같은 트랜잭션에서 analysis_runs.data_warnings 에 쓴다(이슈 #122).
    결과를 저장했고 엔진이 쓴 운영 기준(T8 출력)이 실행 행과 다르면 같은 트랜잭션에서 행을 맞춘다(GLOW-160).
    맞춘 뒤 선택 대기 실행의 운영 기준이 저장하는 날의 활성 버전이 아니면 STALE 로 남기고 그 상태를 돌려준다.
    지연 해소(NO_DELAY)는 엔진 입력을 만들지 않아 경고가 없고 열은 NULL 그대로다.
    """
    saved = await _save_by_outcome(db, run, state, now=now, recalculate=recalculate)
    if saved is not None and saved.saved and "data_warnings" in state:
        await _record_data_warnings(db, run, state["data_warnings"])
    if saved is not None and saved.saved and "operating_rules" in state:
        await _record_engine_operating_rule(db, run, state["operating_rules"])
        if (
            saved.status == AnalysisRunStatus.READY_FOR_REVIEW
            and await _stale_if_operating_rule_not_active(db, run)
        ):
            saved = ResultSaveOutcome(saved=True, status=AnalysisRunStatus.STALE)
    return saved


async def _record_engine_operating_rule(
    db: AsyncSession, run: StartedRun, rules: Any
) -> None:
    """엔진이 계산에 쓴 운영 기준(T8 출력)이 실행 행과 다르면 행을 엔진 쪽으로 맞춘다 (머리말 GLOW-160)."""
    used = getattr(rules, "operating_rule_set_id", None)
    if used is None or uuid.UUID(str(used)) == run.operating_rule_set_id:
        return
    logger.warning(
        "%s analysis_run_id=%s recorded=%s engine=%s engine_version_no=%s",
        OPERATING_RULE_ADJUSTED_LOG_MARK,
        run.analysis_run_id,
        run.operating_rule_set_id,
        used,
        getattr(rules, "version_no", None),
    )
    await db.execute(
        update(AnalysisRun)
        .where(
            AnalysisRun.company_id == run.company_id,
            AnalysisRun.analysis_run_id == run.analysis_run_id,
        )
        .values(operating_rule_set_id=uuid.UUID(str(used)))
    )


async def _stale_if_operating_rule_not_active(
    db: AsyncSession, run: StartedRun
) -> bool:
    """
    선택 대기로 저장한 실행의 운영 기준(엔진 쪽으로 맞춘 뒤)이 끝난 시각 날짜의 활성 버전과 다르면 STALE 로 남긴다
    (머리말 "맞춘 버전이 저장하는 날의 활성 버전이 아니면"). STALE 로 바꿨으면 True.
    현재 실행 버전이 아니거나 지연이 닫힌 실행은 바꾸지 않는다(GLOW-159 와 같은 대상, mark_runs_stale 의 동결 규칙).
    """
    row = await lock_analysis_run(db, run.analysis_run_id)
    if (
        row is None
        or not row.is_latest
        or row.status != AnalysisRunStatus.READY_FOR_REVIEW
    ):
        return False
    active = await RunCreationRepository(db).find_operating_rule_set_id(
        run.company_id, row.finished_at.astimezone(UTC).date()
    )
    if active == row.operating_rule_set_id:
        return False
    changes = StaleChangeRepository(db)
    staled = await StaleJudgmentService(db, changes).mark_runs_stale(
        [run.analysis_run_id],
        StaleReasonCode.OPERATING_RULE_CHANGED,
        await changes.find_max_applied_seq(run.company_id),
        row.finished_at,
    )
    if staled:
        logger.warning(
            "%s analysis_run_id=%s computed=%s active=%s",
            OPERATING_RULE_NOT_ACTIVE_LOG_MARK,
            run.analysis_run_id,
            row.operating_rule_set_id,
            active,
        )
    return bool(staled)


async def _record_data_warnings(
    db: AsyncSession, run: StartedRun, warnings: list[dict[str, Any]]
) -> None:
    await db.execute(
        update(AnalysisRun)
        .where(
            AnalysisRun.company_id == run.company_id,
            AnalysisRun.analysis_run_id == run.analysis_run_id,
        )
        .values(data_warnings=[dict(w) for w in warnings])
    )


async def _save_by_outcome(
    db: AsyncSession,
    run: StartedRun,
    state: Mapping[str, Any],
    *,
    now: datetime | None,
    recalculate: bool,
) -> ResultSaveOutcome | None:
    outcome = state.get("outcome")
    if outcome is not None and outcome != AnalysisOutcome.ON_HOLD:
        await record_input_data_versions(db, run)
    ids = {"company_id": run.company_id, "analysis_run_id": run.analysis_run_id}
    if outcome in (AnalysisOutcome.READY_FOR_REVIEW, AnalysisOutcome.NO_RISK):
        # 저장 직후 재판정도 판정 서명 재계산을 쓴다(이슈 #75)
        return await save_analysis_result(
            db,
            **ids,
            result=analysis_result_from_state(state),
            recalculator=recalculator_or_none(db) if recalculate else None,
            now=now,
        )
    if outcome == AnalysisOutcome.NO_DELAY:
        no_delay = state["no_delay"]
        return await save_no_delay(
            db,
            **ids,
            decision_signature=no_delay.decision_signature,
            decision_signature_sha256=no_delay.decision_signature_sha256,
            now=now,
        )
    if outcome in (AnalysisOutcome.FAILED, AnalysisOutcome.ON_HOLD):
        summary = state["failure"]
        if summary.failure_code == EXPLANATION_GENERATION_FAILED:
            failed = state["explanation_failed"]
            return await save_analysis_failure(
                db,
                **ids,
                summary=summary,
                computed=analysis_result_from_state(state, with_explanation=False),
                explanation_model=ExplanationModel(
                    failed.model_name, failed.prompt_version
                ),
                now=now,
            )
        return await save_analysis_failure(db, **ids, summary=summary, now=now)
    if outcome == AnalysisOutcome.STALE:
        return None
    raise OutcomeMissingError(
        f"그래프가 종료 상태 없이 끝났습니다 (analysis_run_id={run.analysis_run_id}, outcome={outcome!r})"
    )
