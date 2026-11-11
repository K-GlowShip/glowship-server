import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel

from app.modules.delay_analysis.application.agent.retry import FailureSummary
from app.modules.delay_analysis.application.agent.state import AnalysisStep
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.context import (
    EngineInputLike,
    ToolAttemptTracer,
    ToolContext,
)
from app.modules.delay_analysis.application.agent.tools.registry import (
    ToolSpec,
    call_tool,
)

"""
    분석 실행 하나의 실행 도구 묶음 AnalysisRuntime (GLOW-129, docs/agent-flow/agent-tool-interface.md 5.1절)
      - 그래프 상태에는 데이터(도구 출력·종료 상태)만 두고, 실행에 쓰는 도구(등록표·분석 준비·판정 서명·설명·시각)는
        이 묶음으로 config["configurable"]["analysis_runtime"] 에 넣는다. 실행 상태 조회기(GLOW-98
        run_status_reader)와 같은 방식이다(docs/agent-flow/delay-analysis-graph.md 4절).
      - 엔진 입력은 분석 준비 노드가 실행마다 한 번 만들어 여기에 담는다(start). 상태에 넣지 않는 이유:
        결과가 아니라 입력이고 고객사 전체 스냅샷이라 크다. 상태는 서비스가 받아 저장할 결과만 담는다.
      - 실행 하나에 묶음 하나. 다른 실행에 다시 쓰면 start 가 막는다(ToolContext 도 실행 하나에 하나).
      - 여기서는 노드가 쓰는 모양(Protocol)만 정한다. 실제 함수(엔진 입력 judge_no_delay·build_engine_input·decision_of,
        설명 생성 make_explainer, T12 입력 final_check_input, 실제 도구 12개)를 끼운 묶음은
        run_start/engine_runtime.build_engine_runtime 이 만든다. 시험은 가짜 묶음(fake_runtime)을 쓴다.
"""

RUNTIME_KEY = "analysis_runtime"


@runtime_checkable
class EngineHoldLike(Protocol):
    """엔진 입력 보류(EngineHold). code 는 보류 코드(retry.HOLD_CODES), message 는 판정한 쪽 문구."""

    code: str
    message: str


class AnalysisPreparer(Protocol):
    """분석 준비에 쓰는 엔진 입력 함수 두 개. 실행 하나에 묶여 있고(세션·실행 정보는 만드는 쪽이 가짐) 순서는 노드가 지킨다."""

    async def judge_no_delay(self) -> Any | None:
        """지연이 해소됐으면 NO_DELAY 판정(판정 서명·SHA-256), 아니면 None. 엔진 저장소를 읽기 전에 부른다."""
        ...

    async def build_engine_input(self) -> EngineInputLike | EngineHoldLike:
        """엔진 입력(snap·analysis) 또는 보류."""
        ...


@runtime_checkable
class ExplanationFailedLike(Protocol):
    """설명 3회 실패(설명 생성 ExplanationFailed). 통과(ExplanationPassed)에는 failure_summary 가 없다."""

    model_name: str
    prompt_version: str

    def failure_summary(self) -> FailureSummary: ...


# 설명 생성: (도구 문맥, 생성 시각) → 통과 또는 ExplanationFailedLike. 3회 재시도는 함수 안(인터페이스 2절)
ExplanationGenerator = Callable[[ToolContext, datetime], Awaitable[Any]]
# 판정 서명·의존 범위(엔진 입력 decision_of): 엔진 입력 → 판정 결과(저장에 그대로 넘김)
DecisionMaker = Callable[[EngineInputLike], Any]
# 최종 확인(T12) 입력 만들기: 통과한 설명 → T12 입력 모델
FinalCheckInput = Callable[[Any], BaseModel]


class RuntimeWiringError(RuntimeError):
    """
    실행 도구 묶음을 잘못 연결함: config 에 없음·다른 형식·실행 둘에 씀·분석 준비 전에 도구 문맥을 씀.
    분석 실행 하나의 실패가 아니라 부르는 코드의 연결 버그라, 노드가 실패 요약(FAILED)으로 바꾸지 않고
    그대로 올린다(사용자 결정 11-07, 이슈 #57 1번). 그 실행은 자동 시작의 마지막 잡기가 FAILED 로 남긴다(#51).
    """


def utc_now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


@dataclass
class AnalysisRuntime:
    registry: Mapping[ToolName, ToolSpec]
    preparer: AnalysisPreparer
    decide: DecisionMaker
    explain: ExplanationGenerator
    final_check_input: FinalCheckInput
    input_seq: int  # 실행 입력 순번 (#21)
    tracer: ToolAttemptTracer | None = None
    clock: Callable[[], datetime] = utc_now_seconds  # 설명 생성 시각(UTC, 초 단위)
    sleep: Callable[[float], Awaitable[None]] = (
        asyncio.sleep
    )  # 재시도 사이 대기(시험에서 바꿈)
    _ctx: ToolContext | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        missing = [name.value for name in ToolName if name not in self.registry]
        if missing:  # 그래프를 돌리기 전에 알린다(등록표 require 와 같은 뜻)
            raise ValueError(f"등록되지 않은 도구: {missing}")

    def start(
        self,
        *,
        company_id: uuid.UUID,
        analysis_run_id: uuid.UUID,
        engine: EngineInputLike,
    ) -> ToolContext:
        if self._ctx is not None:
            raise RuntimeWiringError(
                "실행 도구 묶음은 실행 하나에만 씁니다(엔진 입력을 이미 담음)"
            )
        # 기준일 D 는 엔진 입력이 정한 값을 쓴다(EngineInput.base_date, #67). 실행 날짜와 다를 수 있다:
        # 그날 재고·주문 자료가 입력에 없으면 마지막 자료 날로 당겨진다. 실행 날짜를 넣으면 T1 입력 확인이 실패한다.
        self._ctx = ToolContext(
            company_id=company_id,
            analysis_run_id=analysis_run_id,
            base_date=engine.base_date,
            input_seq=self.input_seq,
            engine=engine,
            tracer=self.tracer,
        )
        return self._ctx

    @property
    def ctx(self) -> ToolContext:
        if self._ctx is None:
            raise RuntimeWiringError("분석 준비(엔진 입력) 전에는 도구 문맥이 없습니다")
        return self._ctx

    async def call(
        self, name: ToolName, payload: BaseModel | None, *, step: AnalysisStep
    ) -> Any:
        return await call_tool(
            self.ctx, name, payload, step=step, registry=self.registry, sleep=self.sleep
        )


def runtime_of(config: RunnableConfig) -> AnalysisRuntime:
    """노드가 실행 도구 묶음을 꺼낸다. 없으면 오류: 설정이 빠진 실행이 빈 결과로 끝나 저장되는 일을 막는다."""
    runtime = (config.get("configurable") or {}).get(RUNTIME_KEY)
    if runtime is None:
        raise RuntimeWiringError(
            f'그래프 실행에 config["configurable"]["{RUNTIME_KEY}"](AnalysisRuntime)가 없습니다'
        )
    if not isinstance(runtime, AnalysisRuntime):
        raise RuntimeWiringError(
            f'config["configurable"]["{RUNTIME_KEY}"] 는 AnalysisRuntime 이어야 합니다: {type(runtime).__name__}'
        )
    return runtime
