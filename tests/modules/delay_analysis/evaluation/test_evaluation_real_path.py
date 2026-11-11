import copy
import os
from collections import Counter

import pytest
from real_path_support import (
    ANSWER_COLUMNS,
    RESULT_COLUMNS,
    ROW_PARTS,
    answer_runs,
    answer_snapshot,
    differences,
    fix_explanation_canary,
    pick_runs,
    require_engine,
    rerun,
    state_problems,
)

from app.modules.delay_analysis.application.run_start.engine_runtime import (
    missing_engine_tools,
)

"""
    정답 실행을 실제 경로로 다시 돌려 저장 행을 정답과 대조 (GLOW-58 2단계 A 갈래)
      - 실제 경로: 실제 도구 12개 묶음(build_engine_runtime) → 실행기(AnalysisRunner) → save_graph_outcome, 워커 역할.
        분석 자동 시작(docs/analysis/auto-start.md)이 켜졌을 때 도는 길과 같다.
      - 대표 6건(READY·NO_RISK·NO_DELAY·보류·RT-1·RT-2)은 늘 돈다. 전체 21건은 GLOWSHIP_ENGINE_GOLDEN=1 일 때만
        (test_engine_input.py 의 정답 대조와 같은 스위치, 몇 분).
      - 견주는 것: 결과 열·실패 요약 4열·결과 행 4종(재고 영향·위험 주문·대응안·주문 결과). 실패 문구와 상태·STALE 열은
        견주지 않는다(real_path_support 머리말).
      - 정답의 장애 주입 2건(RT-1 T5 시간 초과, RT-2 설명 결함 3개)은 같은 주입으로 재현하고, 주입을 빼면 READY 로
        끝나는 것도 본다(주입이 실패의 원인임을 보이는 대조).
      - 한 시험이 실행을 (고객사, 입력 순번) 순으로 이어 돌린다. 시험마다 엔진 저장소 캐시가 비워져(conftest),
        실행별 시험으로 나누면 고객사 저장소(약 10초)를 매번 다시 읽기 때문이다. 실패는 실행별로 모아 한 번에 보인다.
"""

RT1, RT2 = "TOOL_TIMEOUT_EXHAUSTED", "EXPLANATION_GENERATION_FAILED"
# (고객사 코드, run_code) → 정답의 완료 상태 또는 실패 코드
REPRESENTATIVE = {
    ("A_BEAUTY", "AN-260107-001"): RT2,  # 입력 순번 40
    ("A_BEAUTY", "AN-260110-001"): RT1,  # 55
    ("A_BEAUTY", "AN-260115-001"): "READY_FOR_REVIEW",  # 80, 위험 주문 7건
    ("A_BEAUTY", "AN-260115-002"): "NO_RISK",  # 80 (저장소 캐시 적중)
    ("B_COSMETICS", "AN-260102-001"): "NO_DELAY",  # 15
    ("B_COSMETICS", "AN-260102-002"): "ON_HOLD",  # 15
}
ANSWER_COUNTS = {
    "READY_FOR_REVIEW": 13,
    "NO_RISK": 4,
    "NO_DELAY": 1,
    "ON_HOLD": 1,
    "FAILED": 2,
}
MAX_SECONDS = 60  # 느슨한 상한. NFR-001(30초)은 PC 마다 달라 여기서 단정하지 않는다(문서에 측정값)


def _golden() -> bool:
    return os.environ.get("GLOWSHIP_ENGINE_GOLDEN") == "1"


def test_real_runtime_has_every_contract_tool():
    """실제 등록표에 계약 도구 12개가 모두 있다(임시 등록 없이 그대로 쓴다)."""
    assert missing_engine_tools() == []


def test_differences_reports_changed_cells():
    """비교 장치가 다른 칸을 실제로 잡는지(늘 '같음'만 내지 않는지)."""
    sku, line = "sku-1", "line-1"
    answer = {
        "run": dict.fromkeys(ANSWER_COLUMNS, 1),
        "inventory": {sku: {"depletion_date": "2026-03-10", "shortage": 5}},
        "risks": {line: {"risk_type": "DELAY_INDUCED"}},
        "options": {},
        "outcomes": {},
    }
    assert differences(answer, copy.deepcopy(answer)) == []

    changed = copy.deepcopy(answer)
    changed["run"]["decision_signature_sha256"] = 2
    changed["inventory"][sku]["shortage"] = 6
    changed["risks"] = {}
    found = differences(answer, changed)

    assert len(found) == 3
    assert "decision_signature_sha256" in found[0]
    assert "['shortage']" in found[1]
    assert "risks" in found[2] and "1건 ≠ 저장 0건" in found[2]


async def _compare(db, runs, problems, counts=None):
    """실행을 차례로 실제 경로로 돌려 정답과 견준다. 다른 곳은 problems 에 모은다."""
    for run in runs:
        answer = await answer_snapshot(db, run)
        result = await rerun(db, run)
        found = differences(answer, result.after) + state_problems(result)
        if result.seconds >= MAX_SECONDS:
            found.append(f"{result.seconds:.1f}초(상한 {MAX_SECONDS}초)")
        problems += [f"{run.label}: {item}" for item in found]
        if counts is not None:
            counts[result.after["run"]["completed_status"]] += 1


async def test_representative_runs_match_answers(db_write_session, monkeypatch):
    """A1·A3·A4: 대표 6건이 정답 행과 같고, RT-1·RT-2 는 주입을 빼면 READY 로 끝난다."""
    if _golden():
        pytest.skip("GLOWSHIP_ENGINE_GOLDEN=1 이면 전체 21건 시험이 대표 6건을 함께 봄")
    require_engine()
    db = db_write_session
    runs = await pick_runs(db, REPRESENTATIVE)
    fix_explanation_canary(monkeypatch)
    problems: list[str] = []

    for run in runs:
        await _compare(db, [run], problems)
        if run.failure_code not in (RT1, RT2):
            continue
        # 주입 없이: 같은 실행이 READY 로 끝난다. RT-2 는 계산 결과 행이 정답(설명 실패로 남긴 계산 결과)과 같다
        answer = await answer_snapshot(db, run)
        clean = await rerun(db, run, inject=False)
        row = clean.after["run"]
        if (row["status"], row["failure_code"]) != ("READY_FOR_REVIEW", None):
            problems.append(f"{run.label} 주입 없음: {row['status']}")
        if (
            run.failure_code == RT2
        ):  # 설명 실패 저장은 의존 범위를 남기지 않아 판정 서명과 결과 행만 견준다
            signature = ("decision_signature", "decision_signature_sha256")
            found = differences(answer, clean.after, columns=signature)
            problems += [f"{run.label} 주입 없음: {item}" for item in found]

    assert not problems, "\n".join(problems)


async def test_all_answer_runs_match_answers(db_write_session, monkeypatch):
    """A2: 전체 21건(스위치). 건수가 정답과 같고 건너뛴 실행이 없다."""
    if not _golden():
        pytest.skip("GLOWSHIP_ENGINE_GOLDEN=1 일 때만 도는 정답 대조(몇 분)라 건너뜀")
    require_engine()
    db = db_write_session
    runs = list((await answer_runs(db)).values())
    await pick_runs(db, REPRESENTATIVE)  # 정답 overlay 가 아닌 DB 면 건너뜀
    fix_explanation_canary(monkeypatch)
    problems: list[str] = []
    counts: Counter = Counter()

    await _compare(db, runs, problems, counts)

    assert not problems, "\n".join(problems)
    assert len(runs) == 21 and dict(counts) == ANSWER_COUNTS


def test_compared_columns_cover_result_and_failure_summary():
    """견주는 열이 결과 열 11개 + 실패 요약 4열이고, 결과 행은 4종이다(조용히 줄지 않게)."""
    assert len(RESULT_COLUMNS) == 11 and len(ANSWER_COLUMNS) == 15
    assert ROW_PARTS == ("inventory", "risks", "options", "outcomes")
