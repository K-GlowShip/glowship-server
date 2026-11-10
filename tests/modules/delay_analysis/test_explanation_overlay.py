import os
from datetime import UTC

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent import explanation as X
from app.modules.delay_analysis.application.agent.tools.context import ToolContext
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.engine import engine_input as EI
from app.modules.delay_analysis.infrastructure.engine import explanation_assets as XA
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

"""
    overlay 설명 회귀 (GLOW-127 후속, docs/agent-flow/explanation.md 6절 3번)
      - 개발 DB 의 overlay 실행으로 엔진 입력을 다시 만들고(build_engine_input, 읽기만) 그래프 연결 감싸기
        make_explainer 로 설명을 다시 만들어, 저장된 설명·모델·프롬프트 판과 같은지 본다.
      - 설명 생성 시각은 저장된 explanation_generated_at 을 그대로 쓴다(설명 metadata.generated_at 까지 같아야 함).
      - 설명 실패 실행(AN-260107-001)은 엔진 하네스가 RT-2 결함(시도 1·2·3)을 넣어 만든 것이다. 같은 결함으로
        다시 만들어 시도 기록이 저장된 failure_message 와 같은지 본다(overlay 는 엔진 시도 기록만 저장).
      - 기본은 READY 1건·실패 1건. 설명이 있는 실행 전체(14건, 엔진 입력 만들기가 실행마다 몇 초)는
        GLOWSHIP_ENGINE_GOLDEN=1 일 때만(test_engine_input.py 의 정답 대조와 같은 방식).
"""

RT2_FAULTS = {
    1: "wrong_number",
    2: "forbidden_phrase",
    3: "timeout",
}  # 엔진 run_s1_s5.RT2_FAULTS
EXPLAINED_RUNS = {  # overlay 에서 설명이 저장된 실행(PASSED 13 + 설명 실패 1), 11-08 개발 DB 확인
    "A_BEAUTY": [
        "AN-260101-001", "AN-260101-002", "AN-260101-003", "AN-260101-004",
        "AN-260102-001", "AN-260103-001", "AN-260107-001", "AN-260115-001",
        "AN-260118-001", "AN-260119-001", "AN-260202-001",
    ],
    "B_COSMETICS": ["AN-260101-001", "AN-260103-002", "AN-260105-001"],
}  # fmt: skip


@pytest.fixture
def engine_ready(monkeypatch):
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 overlay 설명 회귀를 건너뜀")
    generator = load_engine_module("explain.generator")
    monkeypatch.setattr(generator.secrets, "token_hex", lambda n: "ab" * n)
    mock = load_engine_module("explain.mock")
    labels = XA.load_explanation_assets()["labels"]
    return lambda faults=None: mock.MockAdapter(labels, faults or {})


async def _run(session, company_code: str, run_code: str) -> AnalysisRun:
    from app.modules.company.infrastructure.company import Company

    company_id = await session.scalar(
        select(Company.company_id).where(Company.company_code == company_code)
    )
    if company_id is None:
        pytest.skip(f"고객사 {company_code} 가 없는 DB(overlay 미적재)라 건너뜀")
    run = await session.scalar(
        select(AnalysisRun).where(
            AnalysisRun.company_id == company_id, AnalysisRun.run_code == run_code
        )
    )
    if run is None:
        pytest.skip(f"실행 {company_code} {run_code} 가 없는 DB 라 건너뜀")
    return run


async def _regenerate(session, run: AnalysisRun, adapter):
    built = await EI.build_engine_input(session, run)
    assert isinstance(built, EI.EngineInput), (run.run_code, built)
    ctx = ToolContext(
        company_id=run.company_id,
        analysis_run_id=run.analysis_run_id,
        base_date=built.base_date,
        input_seq=built.input_seq,
        engine=built,
    )
    if run.explanation_generated_at is not None:
        now = run.explanation_generated_at.astimezone(UTC)
    else:  # 설명 실패 실행은 생성 시각이 없다. 시도 기록에는 시각이 들어가지 않는다
        now = run.started_at.astimezone(UTC).replace(microsecond=0)
    return await X.make_explainer(adapter=adapter)(ctx, now)


async def _check(session, run: AnalysisRun, make_adapter) -> str:
    if run.explanation_validation_status == "PASSED":
        result = await _regenerate(session, run, make_adapter())
        assert isinstance(result, X.ExplanationPassed), (run.run_code, result)
        assert result.final_explanation_json == run.final_explanation_json, run.run_code
        assert result.generated_at == run.explanation_generated_at, run.run_code
        assert (result.model_name, result.prompt_version) == (
            run.model_name,
            run.prompt_version,
        ), run.run_code
        return "PASSED"
    assert run.failure_code == "EXPLANATION_GENERATION_FAILED", run.run_code
    result = await _regenerate(session, run, make_adapter(RT2_FAULTS))
    assert isinstance(result, X.ExplanationFailed), (run.run_code, result)
    assert result.failure_message == run.failure_message, run.run_code
    assert (result.model_name, result.prompt_version) == (
        run.model_name,
        run.prompt_version,
    ), run.run_code
    return "FAILED"


async def test_ready_overlay_run_explanation_is_regenerated_exactly(
    db_session, engine_ready
):
    run = await _run(db_session, "A_BEAUTY", "AN-260101-002")
    assert await _check(db_session, run, engine_ready) == "PASSED"


async def test_explanation_failed_overlay_run_has_same_attempt_record(
    db_session, engine_ready
):
    run = await _run(db_session, "A_BEAUTY", "AN-260107-001")
    assert await _check(db_session, run, engine_ready) == "FAILED"


async def test_changed_stored_explanation_is_caught(db_session, engine_ready):
    """음성: 저장 설명 한 글자를 바꾼 사본과는 같지 않다(비교가 실제로 내용을 본다)."""
    run = await _run(db_session, "A_BEAUTY", "AN-260101-002")
    result = await _regenerate(db_session, run, engine_ready())
    changed = dict(run.final_explanation_json)
    changed["risk_summary"] = changed["risk_summary"] + " "
    assert result.final_explanation_json == run.final_explanation_json
    assert result.final_explanation_json != changed


@pytest.mark.parametrize("company_code", sorted(EXPLAINED_RUNS))
async def test_all_explained_overlay_runs_golden(
    db_session, engine_ready, company_code
):
    if os.environ.get("GLOWSHIP_ENGINE_GOLDEN") != "1":
        pytest.skip("GLOWSHIP_ENGINE_GOLDEN=1 일 때만 도는 정답 대조(몇 분)라 건너뜀")
    paths = [
        await _check(
            db_session, await _run(db_session, company_code, code), engine_ready
        )
        for code in EXPLAINED_RUNS[company_code]
    ]
    assert paths.count("FAILED") == (1 if company_code == "A_BEAUTY" else 0)
