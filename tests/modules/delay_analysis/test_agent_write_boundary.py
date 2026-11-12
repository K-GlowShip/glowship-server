import ast
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import MOCK_EXPLANATION_MODEL, get_settings
from app.core.rls_context import set_rls_context
from app.modules.company.infrastructure.company import Company
from app.modules.delay_analysis.application.agent.graph import build_analysis_graph
from app.modules.delay_analysis.application.agent.runtime import RUNTIME_KEY
from app.modules.delay_analysis.application.agent.state import AnalysisOutcome
from app.modules.delay_analysis.application.run_start.engine_runtime import (
    build_engine_runtime,
)
from app.modules.delay_analysis.application.run_start.runner import initial_state
from app.modules.delay_analysis.application.run_start.transitions import StartedRun
from app.modules.delay_analysis.infrastructure.analysis_enums import CompletedStatus
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)
from app.modules.delay_analysis.infrastructure.repository.run_status_repository import (
    AnalysisRunStatusReader,
)

"""
    에이전트는 업무 DB 에 쓰지 않고 대응안을 확정하지 않는다 (AIR-007, agentflow "에이전트가 하지 않는 것")
      1. 닫힌 import: 에이전트 모듈(application/agent/**)을 모두 불러와도 DB 라이브러리·DB 세션·저장소·저장 서비스·
         판단(초안·최종 선택) 모듈이 올라오지 않는다. 쓰는 코드를 직접 넣으면(import) 이 시험이 깨진다.
         검사기 자체도 시험한다: 저장 서비스를 하나 더 불러오게 하면 잡는다.
      2. 확정 수단 없음: 에이전트 코드는 초안·최종 선택 모델과 FINALIZED 를 모른다. 에이전트가 내는 종료 상태에
         FINALIZED 가 없고, 초안·최종 선택 행을 만드는 곳은 판단 모듈(app/modules/decision) 뿐이다.
      3. 읽기 전용 실행: 실행기가 주입하는 실제 묶음(분석 준비·도구 12개·판정·설명·검증)과 실행 상태 조회기를
         READ ONLY 트랜잭션 하나로 만들어 실제 실행을 그래프 끝까지 돌린다. 어디서든 쓰기를 시도하면 PostgreSQL 이
         거부해 이 시험이 실패한다(구조 검사로는 못 보는, 주입받은 함수의 쓰기를 막음). 그 거부가 실제로 걸려
         있는지도 따로 본다.
      - 범위 밖: 메일 추출값 반영·안내문 발송은 설계상 MVP 밖(serviceflow 7절 Should, 발송 없음)이라 서버에 그 기능이 없다.
        추적 로그 파일 쓰기는 요구가 허용한다("분석 로그 저장은 개별 승인 없이 수행").
"""

ROOT = Path(__file__).resolve().parents[3]
AGENT_DIR = ROOT / "app" / "modules" / "delay_analysis" / "application" / "agent"

# 에이전트 모듈과 함께 올라오면 안 되는 것: DB 라이브러리, DB 세션, 저장소·저장·전이 서비스, 판단 모듈
FORBIDDEN_PREFIXES = (
    "sqlalchemy",
    "asyncpg",
    "psycopg",
    "psycopg2",
    "alembic",
    "app.core.database",
    "app.core.rls_context",
    "app.modules.decision",
    "app.modules.delay_analysis.application.result_save",
    "app.modules.delay_analysis.application.run_start",
    "app.modules.delay_analysis.application.run_creation",
    "app.modules.delay_analysis.application.stale",
)
FORBIDDEN_PARTS = (".repository",)  # 어느 모듈이든 저장소 패키지

DECISION_NAMES = {"FinalDecision", "MemberDraft", "NewFinalDecision", "NewMemberDraft"}

_PROBE = """
import importlib, json, sys
for name in json.loads(sys.argv[1]):
    importlib.import_module(name)
print(json.dumps(sorted(sys.modules)))
"""


def _agent_modules() -> list[str]:
    return sorted(
        ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for path in AGENT_DIR.rglob("*.py")
        if path.name != "__init__.py"
    )


def _loaded_after_import(modules: list[str]) -> list[str]:
    """새 프로세스에서 modules 를 불러온 뒤 올라온 모듈 전부(이 시험 프로세스는 이미 DB 모듈을 불렀으므로 따로 띄운다)."""
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, json.dumps(modules)],
        check=False,  # 실패는 아래에서 stderr 와 함께 보인다
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _forbidden(loaded: list[str]) -> list[str]:
    return [
        name
        for name in loaded
        if name.startswith(FORBIDDEN_PREFIXES)
        or any(part in name for part in FORBIDDEN_PARTS)
    ]


# --- 1. 닫힌 import ------------------------------------------------------------------------------


def test_agent_modules_load_no_database_or_saving_code():
    modules = _agent_modules()
    assert len(modules) >= 20, (
        modules
    )  # 폴더를 잘못 찾아 빈 목록을 검사하는 일을 막는다

    loaded = _loaded_after_import(modules)

    assert set(modules) <= set(loaded)
    assert _forbidden(loaded) == []


def test_import_check_catches_saving_code_next_to_the_agent():
    """검사기 음성: 에이전트 모듈 옆에 저장 서비스 하나를 더 불러오면 잡는다(DB 라이브러리까지 따라 올라옴)."""
    saving = "app.modules.delay_analysis.application.result_save.result_save_service"

    found = _forbidden(_loaded_after_import([*_agent_modules(), saving]))

    assert saving in found
    assert "sqlalchemy" in found


# --- 2. 확정 수단 없음 ---------------------------------------------------------------------------


def _names_in(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[-1])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


def test_agent_code_knows_no_decision_models_or_finalized():
    for path in sorted(AGENT_DIR.rglob("*.py")):
        names = _names_in(ast.parse(path.read_text(encoding="utf-8")))
        assert not names & DECISION_NAMES, path
        assert "FINALIZED" not in names, path


def test_agent_outcomes_have_no_finalized():
    assert "FINALIZED" not in {s.value for s in AnalysisOutcome}
    assert {s.value for s in CompletedStatus} == {
        "READY_FOR_REVIEW",
        "NO_RISK",
        "NO_DELAY",
        "ON_HOLD",
        "FAILED",
    }


def test_only_the_decision_module_creates_drafts_and_final_decisions():
    makers: set[str] = set()
    for path in sorted((ROOT / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in DECISION_NAMES
            ):
                makers.add(path.relative_to(ROOT).as_posix())
    assert makers, "만드는 곳을 하나도 못 찾으면 검사가 헛돈다"
    assert all(m.startswith("app/modules/decision/") for m in makers), makers


# --- 3. 읽기 전용 실행 ---------------------------------------------------------------------------

# 개발 DB(2026 H1)의 최종 선택까지 끝난 실행. 상태가 STALE 이 아니어서 노드 경계 확인에서 멈추지 않는다
FINALIZED_RUN = ("A_BEAUTY", "AN-260101-002")


@pytest.fixture
def mock_engine(monkeypatch):
    settings = get_settings()
    if settings.glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 실제 묶음 시험을 건너뜀")
    if settings.explanation_model != MOCK_EXPLANATION_MODEL:
        pytest.skip(
            "EXPLANATION_MODEL 이 mock 이 아니라 건너뜀(저장된 설명은 엔진 템플릿 설명)"
        )
    generator = load_engine_module("explain.generator")
    monkeypatch.setattr(generator.secrets, "token_hex", lambda n: "ab" * n)


async def _stored_run(db) -> AnalysisRun:
    company_code, run_code = FINALIZED_RUN
    company_id = await db.scalar(
        select(Company.company_id).where(Company.company_code == company_code)
    )
    run = None
    if company_id is not None:
        run = await db.scalar(
            select(AnalysisRun).where(
                AnalysisRun.company_id == company_id, AnalysisRun.run_code == run_code
            )
        )
    if run is None:
        pytest.skip(f"실행 {company_code} {run_code} 가 없는 DB 라 건너뜀")
    return run


async def test_real_runtime_runs_the_graph_in_a_read_only_transaction(
    db_session, mock_engine
):
    db = db_session
    assert await db.scalar(text("SHOW transaction_read_only")) == "on"
    run = await _stored_run(db)
    stored_explanation = run.final_explanation_json
    stored_recommended = await db.scalar(
        text(
            "SELECT option_type FROM response_options"
            " WHERE analysis_run_id = :r AND is_recommended"
        ),
        {"r": run.analysis_run_id},
    )
    started = StartedRun.of(run)
    generated_at = run.explanation_generated_at.astimezone(UTC)

    @asynccontextmanager
    async def same_session():
        yield db

    await set_rls_context(db, user_id=None, company_id=run.company_id)
    runtime = build_engine_runtime(db, started, clock=lambda: generated_at)
    try:
        state = await build_analysis_graph().ainvoke(
            initial_state(started),
            {
                "configurable": {
                    RUNTIME_KEY: runtime,
                    "run_status_reader": AnalysisRunStatusReader(
                        same_session, run.company_id
                    ),
                }
            },
        )
    finally:
        runtime.release()

    assert state["outcome"] == AnalysisOutcome.READY_FOR_REVIEW
    assert state["visited_nodes"][-1] == "validate"
    assert state["explanation"].final_explanation_json == stored_explanation
    recommended = [o for o in state["option_ranking"].options if o.is_recommended]
    assert [o.option_type for o in recommended] == [stored_recommended]
    # 끝까지 같은 읽기 전용 트랜잭션이었다(쓰기가 있었다면 위에서 오류로 멈춤)
    assert await db.scalar(text("SHOW transaction_read_only")) == "on"


async def test_read_only_transaction_really_rejects_writes(db_session):
    """장치 음성: 위 시험의 방어가 실제로 걸려 있다(쓰기 한 줄이 거부된다)."""
    with pytest.raises(DBAPIError, match="read-only transaction"):
        await db_session.execute(
            text("UPDATE analysis_runs SET status = status WHERE false")
        )
