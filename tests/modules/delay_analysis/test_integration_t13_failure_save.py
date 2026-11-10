import pytest
from sqlalchemy import select, text

from app.modules.delay_analysis.application.agent.retry import (
    EXPLANATION_GENERATION_FAILED,
    REQUIRED_DATA_INVALID,
    TOOL_TIMEOUT_EXHAUSTED,
)
from app.modules.delay_analysis.application.result_save.result_save_service import (
    ExplanationModel,
    save_analysis_failure,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from tests.modules.delay_analysis import integration_support as S
from tests.modules.delay_analysis.test_result_save import (
    _FAILURE_COLUMNS,
    _RESET_FAILURE,
    _SCOPE,
    _clear,
    _result_from_db,
    _snapshot,
)

"""
    통합 시험 T13 저장: 그래프가 만든 실패 요약 → save_analysis_failure → 실제 행
    (agentflow 5.5·7.1 "서비스가 실패 요약 5컬럼 저장", docs/agent-flow/integration-tests-t13-t20.md)
      - 정답 실패 실행 3건(RT-1 도구 시간 초과·RT-2 설명 실패·보류)을 진행 중으로 되돌린 뒤, 연결된 그래프가
        만든 실패 요약을 저장한다. 저장 뒤 행이 원래 정답 행과 같은지 본다(RT-1·RT-2 는 문구만 서버 문구).
      - 실제 UPDATE 라 analysis_runs CHECK 42개는 DB 가 직접 판정한다. 모두 롤백된다.
      - 정답 실행이 없는 DB(빈 DB·데모 시드)는 건너뛴다.
      - 그래프 결과를 저장으로 잇는 실행기(자동 시작, 이슈 #51)는 아직 없어 요약을 직접 넘긴다. 설명 실패의
        계산 결과는 정답 실행의 결과 행에서 되돌린 것이고, 실패 요약·모델 판은 그래프가 만든 것이다
        (그래프 상태 → 계산 결과 변환은 실행기 몫이라 여기서 보지 않는다).
"""

_RUN_BY_CODE = text(
    "SELECT analysis_run_id, company_id FROM analysis_runs"
    " WHERE status IN ('FAILED', 'ON_HOLD') AND failure_code = :code"
    " ORDER BY run_code LIMIT 1"
)
RESULT_TABLES = ("inventory", "risks", "options", "outcomes")
# 정답 실행이 아닌 갈래: 저장 뒤 행이 실패 요약의 열과 같은지만 본다
OTHER_BRANCHES = [
    name
    for name, branch in S.BRANCHES.items()
    if branch.expected.failure
    and not branch.needs_engine
    and name not in ("R01-시간초과3회-T5", "R10-보류-REQUIRED_DATA_INVALID")
]


@pytest.fixture(scope="module")
def engine_kit():
    return S.load_engine_kit()


async def _answer_run(db, code: str):
    row = (await db.execute(_RUN_BY_CODE, {"code": code})).first()
    if row is None:
        pytest.skip(f"정답 실패 실행({code})이 없는 DB 라 건너뜀")
    return row.analysis_run_id, row.company_id


async def _reopen(db, run_id) -> None:
    """실패·보류로 끝난 실행을 진행 중으로 되돌리고 결과 행을 지운다(롤백됨)."""
    await db.execute(text(_RESET_FAILURE), {"run_id": run_id})
    await _clear(db, run_id)


def _without_message(snapshot: dict) -> dict:
    run = {k: v for k, v in snapshot["run"].items() if k != "failure_message"}
    return {**snapshot, "run": run}


async def test_시간_초과_실패_요약을_저장하면_정답_RT1_행과_같고_결과_행이_없다(
    db_write_session,
):
    db = db_write_session
    run_id, company_id = await _answer_run(db, TOOL_TIMEOUT_EXHAUSTED)
    before = await _snapshot(db, run_id, _FAILURE_COLUMNS)
    run = await S.run_branch(S.BRANCHES["R01-시간초과3회-T5"])
    summary = run.result["failure"]

    await _reopen(db, run_id)
    outcome = await save_analysis_failure(
        db, company_id=company_id, analysis_run_id=run_id, summary=summary
    )

    assert (outcome.saved, outcome.status) == (True, "FAILED")
    after = await _snapshot(db, run_id, _FAILURE_COLUMNS)
    assert _without_message(after) == _without_message(before)
    assert after["run"]["failure_message"] == summary.failure_message
    # AIR-009: 실패한 계산의 결과 행은 없다
    assert all(not after[table] for table in RESULT_TABLES)


async def test_보류_실패_요약을_저장하면_정답_보류_행과_전부_같다(db_write_session):
    db = db_write_session
    run_id, company_id = await _answer_run(db, REQUIRED_DATA_INVALID)
    before = await _snapshot(db, run_id, _FAILURE_COLUMNS)
    run = await S.run_branch(S.BRANCHES["R10-보류-REQUIRED_DATA_INVALID"])
    summary = run.result["failure"]

    await _reopen(db, run_id)
    outcome = await save_analysis_failure(
        db, company_id=company_id, analysis_run_id=run_id, summary=summary
    )

    assert (outcome.saved, outcome.status) == (True, "ON_HOLD")
    assert await _snapshot(db, run_id, _FAILURE_COLUMNS) == before


async def test_설명_실패_요약과_모델_판을_저장하면_정답_RT2_행과_같고_계산_결과가_남는다(
    db_write_session, engine_kit, monkeypatch
):
    S.fix_canary(monkeypatch, engine_kit)
    db = db_write_session
    run_id, company_id = await _answer_run(db, EXPLANATION_GENERATION_FAILED)
    before = await _snapshot(db, run_id, _FAILURE_COLUMNS)
    computed = await _result_from_db(
        db,
        run_id,
        completed_status="READY_FOR_REVIEW",
        result_class="FORECAST_ONLY_READY",
        dependency_scope=_SCOPE,
    )
    result = (
        await S.run_branch(S.BRANCHES["R09-설명3회실패-실제"], kit=engine_kit)
    ).result
    summary, failed = result["failure"], result["explanation_failed"]

    await _reopen(db, run_id)
    outcome = await save_analysis_failure(
        db,
        company_id=company_id,
        analysis_run_id=run_id,
        summary=summary,
        computed=computed,
        explanation_model=ExplanationModel(failed.model_name, failed.prompt_version),
    )

    assert (outcome.saved, outcome.status) == (True, "FAILED")
    after = await _snapshot(db, run_id, _FAILURE_COLUMNS)
    # 모델·프롬프트 판·설명 검증 상태·판정 서명·결과 행까지 정답 행과 같다
    assert _without_message(after) == _without_message(before)
    # 문구는 서버 문구 + 엔진 시도 기록. 정답 행의 문구는 그 시도 기록이다
    assert after["run"]["failure_message"] == summary.failure_message
    answer_attempts = before["run"]["failure_message"]
    assert summary.failure_message.endswith(f"(시도: {answer_attempts})")
    assert after["inventory"] and after["options"]


@pytest.mark.parametrize("name", OTHER_BRANCHES)
async def test_그_밖의_실패_요약도_저장하면_행이_요약의_열과_같고_결과_행이_없다(
    db_write_session, name
):
    db = db_write_session
    run_id, company_id = await _answer_run(db, TOOL_TIMEOUT_EXHAUSTED)
    summary = (await S.run_branch(S.BRANCHES[name])).result["failure"]

    await _reopen(db, run_id)
    outcome = await save_analysis_failure(
        db, company_id=company_id, analysis_run_id=run_id, summary=summary
    )

    assert (outcome.saved, outcome.status) == (True, summary.status.value)
    after = await _snapshot(db, run_id, _FAILURE_COLUMNS)
    assert {k: after["run"][k] for k in summary.columns()} == summary.columns()
    assert all(not after[table] for table in RESULT_TABLES)
    finished_at = await db.scalar(
        select(AnalysisRun.finished_at)
        .where(AnalysisRun.analysis_run_id == run_id)
        .execution_options(populate_existing=True)
    )
    assert finished_at is not None
