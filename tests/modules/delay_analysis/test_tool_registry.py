import asyncio
import threading
import uuid
from datetime import date

import pytest
from pydantic import BaseModel

from app.modules.delay_analysis.application.agent.retry import (
    PERMISSION_DENIED,
    TOOL_TEMPORARY_ERROR_EXHAUSTED,
    TOOL_TIMEOUT_EXHAUSTED,
    UNEXPECTED_ERROR_MESSAGE,
    UNEXPECTED_TOOL_ERROR,
    PermissionDeniedError,
    RequiredDataError,
    ToolFailure,
    TransientToolError,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.context import ToolContext
from app.modules.delay_analysis.application.agent.tools.registry import (
    ToolRegistry,
    ToolSpec,
    call_tool,
    pure_tool,
)

"""
    도구 등록표·call_tool 시험 (GLOW-129, docs/agent-flow/agent-tool-interface.md 5절)
    call_tool 의 갈래마다 하나씩: 성공, 등록 안 됨, 입력·출력 형식, 일시 오류 3회, 시간 초과, 권한, 그 밖의 예외,
    보류 → FAILED(결정 1), 추적 훅, 부르는 노드의 단계, 순수 함수 래퍼, 등록표 확인.
"""


class In(BaseModel):
    value: int


class Out(BaseModel):
    doubled: int


class Other(BaseModel):
    x: int = 0


class _Engine:
    def __init__(self):
        self.snap: dict = {}
        self.analysis: dict = {}


class _Tracer:
    def __init__(self):
        self.records = []

    def tool_attempt(self, record):
        self.records.append(record)


def _ctx(tracer=None):
    return ToolContext(
        company_id=uuid.uuid4(),
        analysis_run_id=uuid.uuid4(),
        base_date=date(2026, 1, 2),
        input_seq=10,
        engine=_Engine(),
        tracer=tracer,
    )


_DEFAULT = In(value=2)


async def _no_sleep(_):
    return None


def _call(
    fn,
    payload=_DEFAULT,
    *,
    input_model=In,
    output_model=Out,
    step=AnalysisStep.ANALYZING_IMPACT,
    timeout=None,
    ctx=None,
):
    registry = ToolRegistry(
        {ToolName.GET_DELAY_CONTEXT: ToolSpec(fn, input_model, output_model, timeout)}
    )
    return asyncio.run(
        call_tool(ctx or _ctx(), ToolName.GET_DELAY_CONTEXT, payload, step=step, registry=registry, sleep=_no_sleep)
    )  # fmt: skip


def _failure(*args, **kwargs):
    with pytest.raises(ToolFailure) as caught:
        _call(*args, **kwargs)
    return caught.value.summary


def _caught(*args, **kwargs) -> ToolFailure:
    with pytest.raises(ToolFailure) as caught:
        _call(*args, **kwargs)
    return caught.value


async def _double(ctx, payload):
    return Out(doubled=payload.value * 2)


def test_success_returns_output_model():
    assert _call(_double) == Out(doubled=4)


def test_unregistered_tool_is_blocked():
    registry = ToolRegistry({})
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(
            call_tool(
                _ctx(),
                ToolName.COMPARE_OPTIONS,
                None,
                step=AnalysisStep.VALIDATING_OUTPUT,
                registry=registry,
            )
        )
    s = caught.value.summary
    assert (s.status, s.failure_code, s.attempt_count, s.retry_exhausted) == (
        AnalysisOutcome.FAILED,
        UNEXPECTED_TOOL_ERROR,
        1,
        False,
    )
    # Client 문구는 고정, 도구 이름과 사유는 서버 로그로 갈 detail 에만(이슈 #57 2번)
    assert s.failure_message == UNEXPECTED_ERROR_MESSAGE
    assert caught.value.detail == "compare_options: 등록되지 않은 도구"


def test_wrong_input_and_output_types_are_code_errors():
    calls = []

    async def spy(ctx, payload):
        calls.append(payload)
        return Out(doubled=0)

    for summary in (
        _failure(spy, payload=Other()),  # 입력 모델이 다름
        _failure(spy, payload=In(value=1), input_model=None),  # 입력이 없는 도구에 입력
        _failure(spy, output_model=Other),  # 출력 모델이 다름
    ):
        assert (
            summary.failure_code,
            summary.attempt_count,
            summary.retry_exhausted,
        ) == (UNEXPECTED_TOOL_ERROR, 1, False)
    assert len(calls) == 1  # 입력이 틀리면 도구를 부르지 않는다(세 번째만 불림)
    assert _call(spy, payload=None, input_model=None) == Out(
        doubled=0
    )  # 입력 없음 + None 은 정상


def test_transient_errors_retry_three_times_then_fail():
    calls = []

    async def flaky(ctx, payload):
        calls.append(1)
        if len(calls) < 3:
            raise TransientToolError("원천 응답 지연")
        return Out(doubled=1)

    assert _call(flaky) == Out(doubled=1) and len(calls) == 3  # 3번째에 성공
    calls.clear()

    async def always(ctx, payload):
        calls.append(1)
        raise TransientToolError("계속 실패")

    s = _failure(always)
    assert (s.failure_code, s.attempt_count, s.retry_exhausted, len(calls)) == (
        TOOL_TEMPORARY_ERROR_EXHAUSTED,
        3,
        True,
        3,
    )


def test_timeout_is_retried_without_the_tool_raising():
    async def slow(ctx, payload):
        await asyncio.sleep(1)
        return Out(doubled=0)

    s = _failure(slow, timeout=0.01)
    assert (s.failure_code, s.attempt_count, s.retry_exhausted) == (
        TOOL_TIMEOUT_EXHAUSTED,
        3,
        True,
    )


def test_permission_and_unexpected_errors_are_not_retried():
    calls = []

    async def denied(ctx, payload):
        calls.append(1)
        raise PermissionDeniedError("다른 고객사")

    async def broken(ctx, payload):
        calls.append(1)
        raise KeyError("bug")

    assert _failure(denied).failure_code == PERMISSION_DENIED
    assert _failure(broken).failure_code == UNEXPECTED_TOOL_ERROR
    assert len(calls) == 2


def test_required_data_error_after_prepare_is_failed_not_hold():
    """결정 1: 보류는 엔진 입력 단계에서만. 도구의 RequiredDataError 는 FAILED"""

    async def missing(ctx, payload):
        raise RequiredDataError("MISSING_REQUIRED_DATA", "영향 SKU 가 입력에 없음")

    failure = _caught(missing, step=AnalysisStep.ANALYZING_OPTIONS)
    s = failure.summary
    assert (s.status, s.failed_step, s.failure_code, s.attempt_count) == (
        AnalysisOutcome.FAILED,
        AnalysisStep.ANALYZING_OPTIONS,
        UNEXPECTED_TOOL_ERROR,
        1,
    )
    assert s.failure_message == UNEXPECTED_ERROR_MESSAGE
    assert "MISSING_REQUIRED_DATA 영향 SKU 가 입력에 없음" in failure.detail


def test_step_comes_from_calling_node():
    async def denied(ctx, payload):
        raise PermissionDeniedError()

    assert (
        _failure(denied, step=AnalysisStep.ANALYZING_IMPACT).failed_step
        is AnalysisStep.ANALYZING_IMPACT
    )
    assert (
        _failure(denied, step=AnalysisStep.ANALYZING_OPTIONS).failed_step
        is AnalysisStep.ANALYZING_OPTIONS
    )


def test_tracer_gets_every_attempt():
    tracer, calls = _Tracer(), []

    async def flaky(ctx, payload):
        calls.append(1)
        if len(calls) == 1:
            raise TransientToolError()
        return Out(doubled=0)

    _call(flaky, ctx=_ctx(tracer))
    assert [
        (r.tool_name, r.attempt, r.error_code, r.will_retry) for r in tracer.records
    ] == [
        ("get_delay_context", 1, TOOL_TEMPORARY_ERROR_EXHAUSTED, True),
        ("get_delay_context", 2, None, False),
    ]


def test_pure_function_is_wrapped_and_runs_off_the_event_loop():
    """A 의 대응안 도구처럼 동기 순수 함수(data) -> 출력 을 감싸 등록한다"""
    seen = {}

    def double(data: In) -> Out:
        """두 배"""
        seen["thread"] = threading.current_thread() is threading.main_thread()
        return Out(doubled=data.value * 2)

    wrapped = pure_tool(double)
    assert (wrapped.__name__, wrapped.__doc__) == ("double", "두 배")
    assert _call(wrapped) == Out(doubled=4)
    assert seen["thread"] is False  # 스레드에서 돌았다


def test_registry_accepts_only_contract_names_and_reports_missing():
    spec = ToolSpec(_double, In, Out)
    with pytest.raises(ValueError):
        ToolRegistry({"get_delay_context": spec})  # 문자열은 안 됨, ToolName 만
    registry = ToolRegistry({ToolName.GET_DELAY_CONTEXT: spec})
    assert len(ToolName) == 12
    assert len(registry.missing()) == 11
    registry.require([ToolName.GET_DELAY_CONTEXT])  # 필요한 것만 있으면 통과
    with pytest.raises(ValueError, match="compare_options"):
        registry.require([ToolName.GET_DELAY_CONTEXT, ToolName.COMPARE_OPTIONS])
