import asyncio
import dataclasses
import json
import logging
import time

import pytest

from app.modules.delay_analysis.application.agent.retry import (
    TOOL_TEMPORARY_ERROR_EXHAUSTED,
    TOOL_TIMEOUT_EXHAUSTED,
)
from tests.modules.delay_analysis import integration_support as S
from tests.modules.delay_analysis.test_integration_t13_retry import (
    RT2_ATTEMPTS,
    RT2_MODEL,
)

"""
    통합 시험 T20: 추적 장애 독립성 (AIR-008, agentflow 8.1절 "장애 영향",
    docs/agent-flow/integration-tests-t13-t20.md)
    T13 과 같은 갈래를 추적 장애와 함께 다시 돌려, 추적 없이 돌린 결과와
      1. 그래프 결과 전체(종료 상태·단계·실패 요약·방문 노드·도구 출력)
      2. 도구별 호출 수(재시도 수)·설명 호출 수
      3. 서비스가 저장할 실패 요약의 열
    이 같은지 본다. 장애가 실제로 일어났는지도 함께 확인한다(일어나지 않았다면 빈 시험이다).
    정상 갈래의 쓰기 예외·지연, 마스킹, 내부 로그 형식은 test_agent_tracing 이 본다.
"""

MATRIX_BRANCHES = [
    "R00-정상",
    "R01-시간초과3회-T5",
    "R02-재시도뒤성공-T5",
    "R05-권한-T7",
    "R06-일시오류뒤권한-T1",
    "R09-설명3회실패-실제",
    "R10-보류-REQUIRED_DATA_INVALID",
    "R11-도구밖예외-설명",
    "R12-STALE중단",
]
MATRIX_FAULTS = ("F0", "F1", "F3", "F4", "F5", "F6")  # F2(쓰기 지연)는 따로 본다
SKIPPED = {
    # 이미 있음(test_agent_tracing)
    ("R00-정상", "F1"),
    # 도구·설명 시도가 없어 장애가 날 곳이 없음
    ("R10-보류-REQUIRED_DATA_INVALID", "F4"),
}
MATRIX = [
    (branch, fault)
    for branch in MATRIX_BRANCHES
    for fault in MATRIX_FAULTS
    if (branch, fault) not in SKIPPED
]
MODEL_USAGE_KEYS = {
    "model",
    "response_models",
    "calls",
    "calls_failed",
    "calls_without_usage",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "estimated_cost_usd",
    "cost_complete",
    "price",
}
TOOL_HOOK_FAILED = "도구 시도 기록 실패"  # retry._record
EXPLANATION_HOOK_FAILED = "설명 시도 추적 기록 실패"  # explanation._trace
USAGE_HOOK_FAILED = "모델 사용량 추적 기록 실패"  # explanation._record_model_usage
CALLBACK_FAILED = "추적 콜백 처리 실패"  # TraceCallbackHandler


@pytest.fixture(scope="module")
def engine_kit():
    return S.load_engine_kit()


def _kit(branch: S.Branch, request, monkeypatch):
    if not branch.needs_engine:
        return None
    kit = request.getfixturevalue("engine_kit")
    S.fix_canary(monkeypatch, kit)
    return kit


def _same(run: S.Run, baseline: S.Run) -> None:
    assert run.result == baseline.result
    assert run.calls == baseline.calls
    assert (run.explain_calls, run.model_calls) == (
        baseline.explain_calls,
        baseline.model_calls,
    )
    if "failure" in baseline.result:
        assert run.result["failure"].columns() == baseline.result["failure"].columns()


def _logged(caplog, phrase: str) -> int:
    return sum(phrase in record.getMessage() for record in caplog.records)


def _fault_happened(fault: str, branch: S.Branch, tracer, run: S.Run, caplog) -> None:
    """장애가 실제로 일어났고, 분석 쪽이 그것을 삼켰는지."""
    attempts = sum(run.calls.values())  # 도구 시도 수(도구가 센 값)
    explanation = 1 if branch.needs_engine else 0
    kinds = [event.kind for event in tracer.events]
    if fault == "F0":
        assert tracer.sink_failures == 0
        assert kinds.count("TOOL_ATTEMPT") == attempts  # 추적 기록 = 실제 시도 수
        assert (kinds[0], kinds[-1]) == ("RUN_START", "RUN_END")
    elif fault in ("F1", "F6"):  # 사건마다 쓰기가 실패함
        assert tracer.sink_failures == len(tracer.events) > 0
    elif fault == "F3":  # 사건을 하나도 남기지 못함
        assert kinds == []
        assert _logged(caplog, CALLBACK_FAILED) > 0
        assert _logged(caplog, TOOL_HOOK_FAILED) == attempts
        # 설명 시도 기록과 모델 사용량 기록은 추적기가 예외를 올리지 않고 세기만 한다(설명 한 건에 한 번씩)
        assert tracer.sink_failures == 2 * explanation
    elif fault == "F4":
        assert "TOOL_ATTEMPT" not in kinds and "EXPLANATION_ATTEMPT" not in kinds
        assert "MODEL_USAGE" not in kinds
        assert _logged(caplog, TOOL_HOOK_FAILED) == attempts
        assert _logged(caplog, EXPLANATION_HOOK_FAILED) == explanation
        assert _logged(caplog, USAGE_HOOK_FAILED) == explanation
    elif fault == "F5":  # 콜백이 죽어 실행·노드 사건이 없고, LangChain 이 예외를 삼킴
        assert "RUN_START" not in kinds and "NODE_START" not in kinds
        assert _logged(caplog, "RaisingHandler") > 0


@pytest.mark.parametrize(
    ("name", "fault"), MATRIX, ids=[f"{name}×{fault}" for name, fault in MATRIX]
)
async def test_추적_장애가_있어도_결과와_재시도_횟수와_실패_요약이_추적_없이_돌린_것과_같다(
    name, fault, request, monkeypatch, tmp_path, caplog
):
    branch = S.BRANCHES[name]
    kit = _kit(branch, request, monkeypatch)
    baseline = await S.run_branch(branch, kit=kit)
    tracer, callbacks = S.traced(fault, tmp_path)

    with caplog.at_level(logging.WARNING):
        run = await S.run_branch(branch, tracer=tracer, callbacks=callbacks, kit=kit)
    await tracer.aclose()

    _same(run, baseline)
    _fault_happened(fault, branch, tracer, run, caplog)


@pytest.mark.parametrize("name", ["R01-시간초과3회-T5", "R02-재시도뒤성공-T5"])
async def test_추적_쓰기가_느려도_재시도_갈래의_분석이_기다리지_않는다(name, tmp_path):
    branch = S.BRANCHES[name]
    baseline = await S.run_branch(branch)
    tracer, callbacks = S.traced("F2", tmp_path)

    started = time.perf_counter()
    run = await S.run_branch(branch, tracer=tracer, callbacks=callbacks)
    elapsed = time.perf_counter() - started

    _same(run, baseline)
    # 사건마다 쓰기를 기다렸다면 사건 수 × 쓰는 시간만큼 걸린다
    assert elapsed < S.SLOW_WRITE_SECONDS * len(tracer.events) / 2
    assert tracer.close(timeout=0.01) is False  # 아직 쓰는 중이어도 분석은 끝나 있음
    assert await asyncio.to_thread(tracer.close, 10)  # 나중에라도 다 씀


async def test_비교는_실패_요약_값이_하나만_달라도_다르다고_본다():
    # 위 시험들의 "같다"가 값을 실제로 비교하는지(음성)
    branch = S.BRANCHES["R05-권한-T7"]
    first, second = await S.run_branch(branch), await S.run_branch(branch)
    assert first.result == second.result

    failure = dataclasses.replace(second.result["failure"], attempt_count=2)
    assert first.result != {**second.result, "failure": failure}
    assert first.calls != {**second.calls, S.T.IDENTIFY_AT_RISK_ORDERS: 2}


# --- 실행 ID 하나로 실패 갈래까지 이어짐 (AIR-008) ---------------------------------------------


async def _traced_run(name: str, tmp_path, kit=None):
    tracer, callbacks = S.traced("F0", tmp_path)
    run = await S.run_branch(
        S.BRANCHES[name], tracer=tracer, callbacks=callbacks, kit=kit
    )
    assert tracer.close()
    return run, tracer.events


def _tool_attempts(events, tool: S.T) -> list[tuple]:
    return [
        (e.attempt, e.status, e.error_code)
        for e in events
        if e.kind == "TOOL_ATTEMPT" and e.name == tool.value
    ]


def _one_run(events) -> None:
    """모든 사건이 같은 실행·고객사·추적 ID 이고 순번이 이어진다."""
    assert {(e.analysis_run_id, e.company_id, e.trace_id) for e in events} == {
        (S.RUN_ID, S.COMPANY_ID, S.RUN_ID)
    }
    assert [e.seq for e in events] == list(range(1, len(events) + 1))


async def test_시간_초과로_끝난_실행도_시작부터_재시도_3번과_종료까지_한_실행으로_이어진다(
    tmp_path,
):
    run, events = await _traced_run("R01-시간초과3회-T5", tmp_path)

    _one_run(events)
    assert _tool_attempts(events, S.T.GET_OPEN_ORDERS) == [
        (1, "RETRY", TOOL_TIMEOUT_EXHAUSTED),
        (2, "RETRY", TOOL_TIMEOUT_EXHAUSTED),
        (3, "ERROR", TOOL_TIMEOUT_EXHAUSTED),
    ]
    kinds = [e.kind for e in events]
    assert kinds[0] == "RUN_START"
    # 도구 실패는 노드가 실패 요약으로 끝내므로 오류 사건이 아니라 노드 끝·실행 끝으로 남는다
    assert "NODE_ERROR" not in kinds and "RUN_ERROR" not in kinds
    assert [e.name for e in events if e.kind == "NODE_END"] == [
        node.split(":")[0] for node in run.result["visited_nodes"]
    ]
    end = events[-1]
    assert end.kind == "RUN_END"
    assert (end.summary["outcome"], end.summary["step"]) == (
        "FAILED",
        "ANALYZING_IMPACT",
    )
    assert end.summary["visited_nodes"][-1] == "inventory_impact:failed"


async def test_재시도_뒤_성공한_도구는_재시도_2번과_성공으로_남는다(tmp_path):
    _, events = await _traced_run("R02-재시도뒤성공-T5", tmp_path)

    _one_run(events)
    assert _tool_attempts(events, S.T.GET_OPEN_ORDERS) == [
        (1, "RETRY", TOOL_TIMEOUT_EXHAUSTED),
        (2, "RETRY", TOOL_TEMPORARY_ERROR_EXHAUSTED),
        (3, "OK", None),
    ]
    assert events[-1].summary["outcome"] == "READY_FOR_REVIEW"


async def test_설명_3회_실패는_도구_시도_뒤에_설명_시도_3번으로_남고_정답_시도_기록과_같다(
    engine_kit, monkeypatch, tmp_path
):
    S.fix_canary(monkeypatch, engine_kit)
    _, events = await _traced_run("R09-설명3회실패-실제", tmp_path, engine_kit)

    _one_run(events)
    explained = [e for e in events if e.kind == "EXPLANATION_ATTEMPT"]
    assert [(e.attempt, e.status) for e in explained] == [
        (1, "ERROR"),
        (2, "ERROR"),
        (3, "ERROR"),
    ]
    # 추적의 시도 기록 = 실패 문구에 붙는 엔진 시도 기록 = 정답 RT-2 문구
    recorded = ";".join(
        f"{e.attempt}:{'|'.join(e.summary['codes'])}" for e in explained
    )
    assert recorded == RT2_ATTEMPTS
    assert {(e.summary["model"], e.summary["prompt_version"]) for e in explained} == {
        RT2_MODEL
    }
    kinds = [e.kind for e in events]
    last_tool = len(kinds) - 1 - kinds[::-1].index("TOOL_ATTEMPT")
    assert last_tool < kinds.index("EXPLANATION_ATTEMPT") < kinds.index("RUN_END")
    # 모델 사용량 합계는 설명 시도 뒤·실행 끝 앞에 한 번 남는다(실패한 설명도 호출은 했으므로 남김)
    last_attempt = len(kinds) - 1 - kinds[::-1].index("EXPLANATION_ATTEMPT")
    assert kinds.count("MODEL_USAGE") == 1
    assert last_attempt < kinds.index("MODEL_USAGE") < kinds.index("RUN_END")
    usage = events[kinds.index("MODEL_USAGE")]
    assert (usage.name, usage.status) == ("explain", "OK")
    assert (usage.summary["model"], usage.summary["calls"]) == (RT2_MODEL[0], 3)
    assert _tool_attempts(events, S.T.VALIDATE_AGENT_OUTPUT) == []  # 최종 확인은 안 감
    assert events[-1].summary["outcome"] == "FAILED"


# --- 실패 갈래의 기록에 금지 항목이 없음 (agentflow 8.1 "보내지 않는 것") ----------------------------


def _dump(events) -> str:
    return json.dumps([e.to_json_dict() for e in events], ensure_ascii=False)


@pytest.mark.parametrize(
    "name",
    [
        "R04-일시오류3회-T1",
        "R05-권한-T7",
        "R08-분류안된예외-T6",
        "R11-도구밖예외-설명",
        "R11-도구밖예외-엔진입력",
    ],
)
async def test_실패_갈래의_추적_기록에_예외_내용이_없다(name, tmp_path):
    # 예외 문구에 접속 문자열·주문 코드가 섞여 있어도 기록에는 오류 코드만 남는다
    run, events = await _traced_run(name, tmp_path)

    assert run.result["outcome"] == "FAILED"
    dumped = _dump(events)
    assert "hunter2" not in dumped and "A-ORD-20260613-0040" not in dumped
    assert "postgresql://" not in dumped


async def test_설명_실패_갈래의_추적_기록에_canary와_입력_문서와_업무_값이_없다(
    engine_kit, monkeypatch, tmp_path
):
    S.fix_canary(monkeypatch, engine_kit)
    _, events = await _traced_run("R09-설명3회실패-실제", tmp_path, engine_kit)

    dumped = _dump(events)
    assert S.CANARY not in dumped
    assert "SHIP-DELAYED" not in dumped  # 지연 선적 코드(설명 입력 문서의 업무 값)
    for event in events:
        if event.kind == "EXPLANATION_ATTEMPT":  # 사유 코드·모델·프롬프트 판뿐
            assert set(event.summary) <= {"codes", "model", "prompt_version", "usage"}
        if event.kind == "MODEL_USAGE":  # 모델 이름·수·금액·단가뿐
            assert set(event.summary) == MODEL_USAGE_KEYS
