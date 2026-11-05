import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.modules.delay_analysis.application.agent.retry import (
    UNEXPECTED_TOOL_ERROR,
    FailureSummary,
    ToolFailure,
    call_with_retry,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.context import ToolContext

"""
    도구 등록표와 call_tool (docs/agent-flow/agent-tool-interface.md 5절, GLOW-129)
      - 노드는 도구 함수를 직접 부르지 않고 call_tool 만 쓴다. 등록 확인(AIR-001 "등록된 도구만")·
        입출력 형식 확인·재시도(#28 call_with_retry)·추적 훅이 한 곳에 모인다.
      - 도구 함수 모양: async def 도구(ctx: ToolContext, payload: 계약 입력 모델 | None) -> 계약 출력 모델.
        A 의 대응안 도구처럼 동기 순수 함수(data) -> 출력 이면 pure_tool 로 감싸 등록한다.
      - 실패는 모두 ToolFailure(실패 요약)로 올린다. 형식이 틀린 입력·출력, 등록되지 않은 도구, 그 밖의 예외는
        코드 오류로 보아 재시도 없이 FAILED(UNEXPECTED_TOOL_ERROR).
      - 결정 1(세 세션 합의 11-05): 보류는 엔진 입력 만들기 단계에서만 낸다. 그래서 도구가 RequiredDataError 를
        던져 call_with_retry 가 보류(ON_HOLD) 요약을 만들면, 여기서 FAILED(UNEXPECTED_TOOL_ERROR)로 바꾼다.
"""

ToolFn = Callable[[ToolContext, Any], Awaitable[BaseModel]]


@dataclass(frozen=True)
class ToolSpec:
    fn: ToolFn
    input_model: (
        type[BaseModel] | None
    )  # None 이면 입력 없음(T2~T5·T8, payload 도 None)
    output_model: type[BaseModel]
    timeout_seconds: float | None = (
        None  # 시도마다의 시간 제한. 넘으면 재시도(TOOL_TIMEOUT_EXHAUSTED)
    )


def pure_tool(fn: Callable[[Any], BaseModel]) -> ToolFn:
    """동기 순수 함수 fn(data) -> 출력 을 (ctx, payload) async 도구로 감싼다. 계산이 이벤트 루프를 막지 않게 스레드에서 돈다."""

    async def run(ctx: ToolContext, payload: Any) -> BaseModel:
        return await asyncio.to_thread(fn, payload)

    run.__name__ = getattr(fn, "__name__", "pure_tool")
    run.__doc__ = fn.__doc__
    return run


class ToolRegistry(Mapping[ToolName, ToolSpec]):
    """ToolName → ToolSpec. 계약 도구 이름(ToolName)만 받는다."""

    def __init__(self, specs: Mapping[ToolName, ToolSpec]):
        unknown = [k for k in specs if not isinstance(k, ToolName)]
        if unknown:
            raise ValueError(f"계약에 없는 도구 이름: {unknown}")
        self._specs = dict(specs)

    def __getitem__(self, name: ToolName) -> ToolSpec:
        return self._specs[name]

    def __iter__(self) -> Iterator[ToolName]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def missing(self, required: Sequence[ToolName] = tuple(ToolName)) -> list[ToolName]:
        return [name for name in required if name not in self._specs]

    def require(self, required: Sequence[ToolName] = tuple(ToolName)) -> None:
        """그래프를 만들 때 부를 도구가 모두 등록되었는지 확인한다. 빠지면 ValueError(실행 전에 알림)."""
        missing = self.missing(required)
        if missing:
            raise ValueError(f"등록되지 않은 도구: {[m.value for m in missing]}")


def _code_error(name: str, step: AnalysisStep, attempt: int, why: str) -> ToolFailure:
    return ToolFailure(
        FailureSummary(
            status=AnalysisOutcome.FAILED,
            failed_step=step,
            failure_code=UNEXPECTED_TOOL_ERROR,
            attempt_count=attempt,
            retry_exhausted=False,
            failure_message=f"{name} 도구가 예상하지 못한 오류로 멈췄습니다({why}). 재분석을 요청하고, 다시 나오면 담당자에게 알려 주세요.",
        )
    )


async def call_tool(
    ctx: ToolContext,
    name: ToolName,
    payload: BaseModel | None,
    *,
    step: AnalysisStep,
    registry: Mapping[ToolName, ToolSpec],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> BaseModel:
    """
    등록된 도구를 부른다. step 은 부르는 노드가 정한다(같은 도구도 노드에 따라 실패 단계가 다름, 인터페이스 5절).
    성공하면 출력 모델, 멈추면 ToolFailure(실패 요약 5컬럼 재료)를 올린다.
    """
    spec = registry.get(name)
    if spec is None:  # AIR-001: 등록되지 않은 도구는 부르지 않는다
        raise _code_error(str(name), step, 1, "등록되지 않은 도구")

    async def attempt() -> BaseModel:
        if spec.input_model is None:
            if payload is not None:
                raise TypeError("입력이 없는 도구에 입력을 넘김")
        elif not isinstance(payload, spec.input_model):
            raise TypeError(f"입력은 {spec.input_model.__name__} 이어야 함")
        result = await spec.fn(ctx, payload)
        if not isinstance(result, spec.output_model):
            raise TypeError(f"출력은 {spec.output_model.__name__} 이어야 함")
        return result

    try:
        return await call_with_retry(
            name.value,
            step,
            attempt,
            timeout_seconds=spec.timeout_seconds,
            sleep=sleep,
            on_attempt=ctx.tracer.tool_attempt if ctx.tracer else None,
        )
    except ToolFailure as failure:
        summary = failure.summary
        if (
            summary.status is AnalysisOutcome.ON_HOLD
        ):  # 결정 1: 도구 단계의 보류는 코드 오류
            raise _code_error(
                name.value,
                step,
                summary.attempt_count,
                f"도구가 필수 자료 오류를 냄, 보류는 엔진 입력 단계에서만: {summary.failure_code} {summary.failure_message}",
            ) from failure
        raise
