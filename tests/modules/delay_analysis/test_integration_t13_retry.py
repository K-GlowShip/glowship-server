import logging
import time

import pytest

from app.modules.delay_analysis.application.agent.graph import (
    UNEXPECTED_ERROR_MESSAGE,
)
from app.modules.delay_analysis.application.agent.retry import (
    EXPLANATION_GENERATION_FAILED,
    MISSING_REQUIRED_DATA,
    PERMISSION_DENIED,
    TOOL_TEMPORARY_ERROR_EXHAUSTED,
    TOOL_TIMEOUT_EXHAUSTED,
    UNEXPECTED_TOOL_ERROR,
    tool_label,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from tests.modules.delay_analysis import integration_support as S

"""
    통합 시험 T13: 도구 재시도 제한과 실행 실패 요약 (AIR-009, agentflow 7·7.1절,
    docs/agent-flow/integration-tests-t13-t20.md)
    연결된 그래프 전체(노드 7개·도구 등록표·재시도)에서, 갈래마다
      1. 종료 상태·실패 요약(단계·코드·시도 횟수·소진)·마지막 노드가 기대와 같은지
      2. 도구 12개의 호출 수가 정확히 기대와 같은지(재시도는 최대 2번, 멈춘 뒤 단계의 도구는 0번)
      3. 실패한 계산을 설명(언어 모델)으로 대신하지 않는지
      4. 실패 문구에 도구의 화면 이름·사유·재실행 방법이 있고 내부 이름·예외 내용은 없는지(이슈 #57 2번)
    를 본다. 기대값 가운데 정답 자료에 있는 것은 RT-1(E3 v2)·RT-2(E2 v1)·보류(E9 v1) 3건이고,
    나머지는 재시도 규칙(retry.py)과 agentflow 7절 문장이 근거다.
    재시도 함수·등록표 단위는 test_agent_retry·test_tool_registry 가, 그래프 갈래 하나씩은 test_analysis_graph 가 본다.
"""

# 정답: 05_정답계약검증 정량 정답표 v2 E3 v2·E2 v1, 검증 bundle_verify.json L4.rt (rt1·rt2)
RT1 = (AnalysisStep.ANALYZING_IMPACT, TOOL_TIMEOUT_EXHAUSTED, 3, True)
RT2 = (AnalysisStep.VALIDATING_OUTPUT, EXPLANATION_GENERATION_FAILED, 3, True)
RT2_ATTEMPTS = (
    "1:VALUE_MISMATCH@risk_summary#s1"
    ";2:FORBIDDEN_EXPRESSION@recommendation_explanation:PF-01"
    ";3:MODEL_TIMEOUT@adapter"
)
RT2_MODEL = ("mock/mock-template-v1", "explain_ready_v1@1.0.0")

WITHOUT_ENGINE = [b for b in S.BRANCHES.values() if not b.needs_engine]
# 도구가 멈춘 갈래(도구 실패 문구를 만드는 갈래)
TOOL_FAILURES = [b for b in WITHOUT_ENGINE if b.setup().faults and b.expected.failure]
REASON = {
    TOOL_TIMEOUT_EXHAUSTED: "시간 초과",
    TOOL_TEMPORARY_ERROR_EXHAUSTED: "일시 오류",
    PERMISSION_DENIED: "권한 오류",
    UNEXPECTED_TOOL_ERROR: "예상하지 못한 오류",
}


def _name(branch: S.Branch) -> str:
    return branch.name


@pytest.fixture(scope="module")
def engine_kit():
    return S.load_engine_kit()


def _check(run: S.Run, expected: S.Expected) -> None:
    result = run.result
    assert result["outcome"] == expected.outcome
    assert result["visited_nodes"][-1] == expected.visited_end
    assert run.calls == expected.calls  # 도구 12개 전체: 재시도 수와 "멈춘 뒤 0번"
    assert run.explain_calls == expected.explain_calls
    if expected.explain_calls == 0:  # AIR-009: 실패한 계산을 설명으로 대신하지 않음
        assert "explanation" not in result
    if expected.failure is None:
        assert "failure" not in result
        return
    failure = result["failure"]
    assert S.five(failure) == expected.failure
    assert failure.status == expected.outcome
    assert result["step"] == failure.failed_step
    # 서비스가 그대로 저장할 열: 결과 분류·의존 범위는 비운다(실패한 실행에 결과를 남기지 않음)
    columns = failure.columns()
    assert columns["status"] == columns["completed_status"] == expected.outcome.value
    assert (columns["result_class"], columns["dependency_scope"]) == (None, None)


@pytest.mark.parametrize("branch", WITHOUT_ENGINE, ids=_name)
async def test_갈래마다_종료_상태와_실패_요약과_도구_호출_수가_기대와_같다(
    branch,
):
    _check(await S.run_branch(branch), branch.expected)


async def test_시도별_시간_제한을_3번_넘기면_정답_RT1과_같은_실패_요약으로_끝난다():
    started = time.perf_counter()
    run = await S.run_branch(S.BRANCHES["R01-시간초과3회-T5"])
    elapsed = time.perf_counter() - started

    failure = run.result["failure"]
    assert (failure.status, *S.five(failure)) == (AnalysisOutcome.FAILED, *RT1)
    assert run.calls[S.T.GET_OPEN_ORDERS] == 3
    # 도구는 1초씩 걸리지만 시도마다 0.01초에 끊는다. 3초를 기다렸다면 시간 제한이 걸리지 않은 것
    assert elapsed < 1.0
    assert "3번" in failure.failure_message


@pytest.mark.parametrize("branch", TOOL_FAILURES, ids=_name)
async def test_도구_실패_문구에_화면_이름과_사유와_재실행_방법이_있고_내부_이름과_예외_내용은_없다(
    branch,
):
    (tool,) = branch.setup().faults
    failure = (await S.run_branch(branch)).result["failure"]
    message = failure.failure_message

    assert tool.value not in message  # 내부 함수 이름은 Client 문구에 넣지 않는다
    if failure.failure_code == UNEXPECTED_TOOL_ERROR:
        assert message == UNEXPECTED_ERROR_MESSAGE  # 원인·도구와 상관없이 고정 문구
    else:
        assert message.startswith(tool_label(tool.value))  # 예: "판매량 조회가 …"
    assert REASON[failure.failure_code] in message
    how = "접근 권한" if failure.failure_code == PERMISSION_DENIED else "재분석"
    assert how in message
    assert S.SECRET not in message  # 예외 내용은 Client 문구에 넣지 않는다


async def test_도구가_필수_자료_오류를_내면_보류가_아니라_FAILED이고_그_코드는_서버_로그에만_남는다(
    caplog,
):
    with caplog.at_level(logging.WARNING):
        run = await S.run_branch(S.BRANCHES["R07-도구의필수자료오류-T10"])

    failure = run.result["failure"]
    assert failure.status == AnalysisOutcome.FAILED
    assert failure.failure_message == UNEXPECTED_ERROR_MESSAGE
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(MISSING_REQUIRED_DATA in m for m in warned)


@pytest.mark.parametrize(
    "name", ["R10-보류-REQUIRED_DATA_INVALID", "R10-보류-MISSING_REQUIRED_DATA"]
)
async def test_엔진_입력_보류는_판정한_쪽_문구를_그대로_남긴다(name):
    branch = S.BRANCHES[name]
    failure = (await S.run_branch(branch)).result["failure"]

    assert failure.failure_message == branch.setup().built.message
    assert failure.columns()["status"] == "ON_HOLD"


@pytest.mark.parametrize("name", ["R11-도구밖예외-설명", "R11-도구밖예외-엔진입력"])
async def test_도구_밖_예외는_고정_문구만_남긴다(name):
    failure = (await S.run_branch(S.BRANCHES[name])).result["failure"]

    assert failure.failure_message == UNEXPECTED_ERROR_MESSAGE
    assert S.SECRET not in failure.failure_message


# --- 실제 설명 함수(엔진) ----------------------------------------------------------------------


async def test_실제_설명_함수가_3번_실패하면_정답_RT2와_같은_실패_요약으로_끝난다(
    engine_kit, monkeypatch
):
    S.fix_canary(monkeypatch, engine_kit)
    branch = S.BRANCHES["R09-설명3회실패-실제"]

    run = await S.run_branch(branch, kit=engine_kit)

    _check(run, branch.expected)
    result = run.result
    failure = result["failure"]
    assert (failure.status, *S.five(failure)) == (AnalysisOutcome.FAILED, *RT2)
    assert run.model_calls == 3  # 설명 재생성도 3번을 넘지 않는다
    assert failure.failure_message.endswith(f"(시도: {RT2_ATTEMPTS})")
    failed = result["explanation_failed"]
    assert (failed.model_name, failed.prompt_version) == RT2_MODEL
    # agentflow 5.5 "설명 검증 계속 실패": 계산 결과는 남아 서비스가 함께 저장한다. 통과한 설명은 없다
    assert {"inventory_impact", "option_outcomes", "option_ranking", "decision"} <= set(
        result
    )
    assert "explanation" not in result
    assert failure.columns()["explanation_validation_status"] == "FAILED"


async def test_실제_설명_함수가_통과하면_실제_T12까지_거쳐_READY로_끝난다(
    engine_kit, monkeypatch
):
    # 위 실패 갈래의 대조: 같은 연결에서 결함이 없으면 끝까지 간다(실패가 연결 탓이 아님)
    S.fix_canary(monkeypatch, engine_kit)
    branch = S.Branch(
        "실제-설명-통과",
        lambda: S.Setup(real_explanation=None),
        S.Expected(S.READY, "validate", S.BRANCHES["R00-정상"].expected.calls, None, 1),
        needs_engine=True,
    )

    run = await S.run_branch(branch, kit=engine_kit)

    _check(run, branch.expected)
    assert run.model_calls == 1
