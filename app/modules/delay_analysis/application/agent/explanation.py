import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.modules.delay_analysis.application.agent.retry import (
    FailureSummary,
    explanation_failure,
)
from app.modules.delay_analysis.application.agent.tools.context import ToolContext
from app.modules.delay_analysis.application.agent.tools.validation_schemas import (
    ExplanationMetadata,
    ValidateAgentOutputInput,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import AnalysisRunStatus
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)
from app.modules.delay_analysis.infrastructure.engine.explanation_assets import (
    TEMPLATE_ID,
    TEMPLATE_VERSION,
    ExplanationAssetError,
    load_explanation_assets,
)

"""
    AI 설명 생성 (GLOW-127, docs/agent-flow/explanation.md)
      - 엔진 explain.generator.generate 를 그대로 부른다. 생성기가 자산 사전 검사 → 최대 3시도(모델 호출 1회 + 검증 1회)
        → 시도 기록·실패 문구까지 만든다. 서버는 입력 문서를 만들어 넘기고, 결과를 저장 모양으로 바꾸고, 추적에 넘긴다.
      - 모델은 엔진 MockAdapter(결정적 템플릿)가 기본이다. 실제 외부 모델 호출은 승인되지 않았다(엔진 adapter.py).
      - 설명은 READY_FOR_REVIEW 실행만 만든다(엔진 input_doc.build 규칙). 그 밖의 상태면 모델을 부르지 않고 ValueError.
      - 자산 오류(ExplanationAssetError)는 시도로 세지 않고 실행 상태를 바꾸지 않는다. 호출자에게 그대로 올린다.
      - 실패 원문·canary 는 결과·추적 어디에도 남기지 않는다. 시도 기록 = 사유 코드·위치·모델·프롬프트 판뿐(엔진과 같음).
      - 그래프 연결(GLOW-129 AnalysisRuntime): make_explainer() 가 노드가 부르는 explain(ctx, now) 를,
        final_check_input 이 결과 검증 노드의 T12 입력을 만든다. 실행 묶음에 끼우는 것은 "실제 도구 교체" 작업이다.
"""

logger = logging.getLogger(__name__)


class ExplanationTracer(Protocol):
    """실행 추적기(GLOW-124 RunTracer)의 설명 시도 기록 부분"""

    def explanation_attempts(
        self, attempts: Iterable[Mapping[str, Any]], usage: Iterable[Any] = ()
    ) -> None: ...


@dataclass(frozen=True)
class ExplanationPassed:
    """검증을 통과한 설명. 앞 네 필드가 결과 저장(GLOW-128 Explanation)과 같은 이름이다."""

    model_name: str  # "provider/model_id"
    prompt_version: str  # "template_id@version"
    final_explanation_json: dict[str, Any]  # explanation.v1
    generated_at: (
        datetime  # 호출자가 준 시각(UTC, 초 단위). 모델이 만든 시각을 쓰지 않는다
    )
    input_document: dict[
        str, Any
    ]  # 설명 입력 문서 explanation_input.v1 (T12 로 다시 검증할 때 씀)
    attempts: tuple[dict[str, Any], ...]  # 엔진 시도 기록. 마지막이 통과한 시도

    def expected_metadata(self) -> ExplanationMetadata:
        """T12 에 넘길 기대 메타데이터. 설명 출력이 아니라 입력 문서·시도 기록·생성 시각에서 만든다."""
        run = self.input_document["run"]
        last = self.attempts[-1]
        return ExplanationMetadata(
            schema_version="explanation.v1",
            language="ko",
            analysis_run_id=run["analysis_run_id"],
            decision_signature_hash=run["decision_signature_sha256"],
            model_provider=last["model_provider"],
            model_id=last["model_id"],
            prompt_template_id=last["prompt_template_id"],
            prompt_template_version=last["prompt_template_version"],
            prompt_template_sha256=last["prompt_template_sha256"],
            generated_at=utc_timestamp(self.generated_at),
            retry_no=last["retry_no"],
        )


@dataclass(frozen=True)
class ExplanationFailed:
    """3번 모두 검증에 실패한 설명. 설명 원문은 없다(엔진이 버림)."""

    model_name: str
    prompt_version: str
    failure_message: str  # 엔진 시도 기록 "1:코드@위치|…;2:…;3:…" (1000자 이하)
    attempts: tuple[dict[str, Any], ...]

    def failure_summary(self) -> FailureSummary:
        """실행 실패 요약(#28): FAILED·VALIDATING_OUTPUT·EXPLANATION_GENERATION_FAILED·3회·재시도 소진, 문구 + 시도 기록"""
        return explanation_failure(self.failure_message)


def utc_timestamp(value: datetime) -> str:
    """UTC 'YYYY-MM-DDTHH:MM:SSZ'. 시간대가 없는 시각은 받지 않는다(서버 시각이 UTC 인지 알 수 없음)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("설명 생성 시각은 시간대가 있어야 합니다(UTC)")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_adapter() -> Any:
    """엔진 결정적 mock 모델. 라벨은 해시를 대조한 자산에서 읽는다."""
    mock = load_engine_module("explain.mock")
    return mock.MockAdapter(load_explanation_assets()["labels"])


async def generate_explanation(
    *,
    snap: Mapping[str, Any],
    analysis: Mapping[str, Any],
    analysis_run_id: str,
    shipment_code: str,
    now: datetime,
    adapter: Any | None = None,
    tracer: ExplanationTracer | None = None,
) -> ExplanationPassed | ExplanationFailed:
    """
    snap·analysis 는 엔진 입력 스냅샷과 pipeline.analyze 결과다(GLOW-125 EngineInput.snap·analysis).
    shipment_code 는 지연 선적 코드(설명 첫 문장에 씀). now 는 실행이 정한 설명 생성 시각이다.
    """
    generated_at = utc_timestamp(now)
    if analysis.get("status") != AnalysisRunStatus.READY_FOR_REVIEW:
        raise ValueError(
            f"설명은 READY_FOR_REVIEW 실행만 만듭니다(현재 {analysis.get('status')})"
        )
    input_doc = load_engine_module("explain.input_doc")
    generator = load_engine_module("explain.generator")
    assets = load_engine_module("explain.assets")

    document = input_doc.build(snap, analysis, analysis_run_id, shipment_code)
    model = adapter if adapter is not None else default_adapter()
    try:
        result = await asyncio.to_thread(
            generator.generate,
            document,
            model,
            generated_at,
            template_id=TEMPLATE_ID,
            version=TEMPLATE_VERSION,
        )
    except assets.SystemConfigError as error:
        raise ExplanationAssetError(error.code, error.detail) from error

    attempts = tuple(result["attempts"])
    _trace(tracer, attempts)
    if result["status"] == "PASSED":
        return ExplanationPassed(
            model_name=result["model_name"],
            prompt_version=result["prompt_version"],
            final_explanation_json=result["explanation"],
            generated_at=datetime.strptime(
                result["generated_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=UTC),
            input_document=document,
            attempts=attempts,
        )
    return ExplanationFailed(
        model_name=result["model_name"],
        prompt_version=result["prompt_version"],
        failure_message=result["failure_message"],
        attempts=attempts,
    )


def _trace(
    tracer: ExplanationTracer | None, attempts: tuple[dict[str, Any], ...]
) -> None:
    """추적 장애가 분석 결과를 바꾸지 않게 한다(NFR, GLOW-124 추적 장애 독립)."""
    if tracer is None:
        return
    try:
        tracer.explanation_attempts(attempts)
    except Exception:
        logger.warning("설명 시도 추적 기록 실패(분석은 계속)", exc_info=True)


# --- 그래프 연결 (GLOW-129 AnalysisRuntime.explain·final_check_input, 인터페이스 문서 2절) ---------------------


class ExplanationInputError(ValueError):
    """도구 문맥으로 설명 입력을 만들 수 없음(연결 오류). 실행 ID 가 다르거나 지연 선적 코드가 저장소에 없음."""


class ExplainEngineInput(Protocol):
    """설명이 읽는 엔진 입력 칸(GLOW-125 EngineInput). ToolContext.engine 의 실제 값이 갖는다."""

    analysis_run_id: uuid.UUID
    shipment_id: uuid.UUID
    snap: Mapping[str, Any]
    analysis: Mapping[str, Any]
    store: Any  # 엔진 Store. 선적 표 ships[shipment_id 문자열] 에 shipment_code 가 있다


def delayed_shipment_code(engine: ExplainEngineInput) -> str:
    """지연 선적 코드(설명 첫 문장). 엔진 하네스(run_s1_s5 explain_run)처럼 저장소 선적 표에서 꺼낸다."""
    ships = getattr(engine.store, "ships", None)
    row = ships.get(str(engine.shipment_id)) if isinstance(ships, Mapping) else None
    code = row.get("shipment_code") if isinstance(row, Mapping) else None
    if not isinstance(code, str) or not code:
        raise ExplanationInputError(
            f"엔진 저장소 선적 표에 지연 선적 {engine.shipment_id} 의 코드가 없습니다"
        )
    return code


ExplanationResult = ExplanationPassed | ExplanationFailed


def make_explainer(
    *, adapter: Any | None = None
) -> Callable[[ToolContext, datetime], Awaitable[ExplanationResult]]:
    """
    그래프 설명 노드가 부르는 explain(ctx, now) (AnalysisRuntime.explain). 엔진 계산은 다시 하지 않고
    ctx.engine 의 snap·analysis 를 그대로 쓴다. 추적은 ctx.tracer 가 설명 시도 기록을 받을 때만(GLOW-124 RunTracer).
    adapter 는 generate_explanation 과 같다(기본 엔진 MockAdapter).
    """

    async def explain(ctx: ToolContext, now: datetime) -> ExplanationResult:
        engine = ctx.engine
        if engine.analysis_run_id != ctx.analysis_run_id:
            raise ExplanationInputError(
                f"엔진 입력 실행 {engine.analysis_run_id} 이 도구 문맥 실행 {ctx.analysis_run_id} 과 다릅니다"
            )
        tracer = ctx.tracer
        if not callable(getattr(tracer, "explanation_attempts", None)):
            tracer = None
        return await generate_explanation(
            snap=engine.snap,
            analysis=engine.analysis,
            analysis_run_id=str(ctx.analysis_run_id),
            shipment_code=delayed_shipment_code(engine),
            now=now,
            adapter=adapter,
            tracer=tracer,
        )

    return explain


def final_check_input(passed: ExplanationPassed) -> ValidateAgentOutputInput:
    """
    결과 검증 노드의 T12 입력(AnalysisRuntime.final_check_input). 출력 원문은 통과한 설명 JSON 을 다시 문자열로
    만든 것이다(엔진 생성기는 통과한 원문을 따로 돌려주지 않음, 같은 설명이면 판정이 같음). canary 는 시도마다
    엔진이 만들고 버려 알 수 없으므로 null(유출 검사 하나를 건너뜀, 계약 8절).
    """
    return ValidateAgentOutputInput(
        input_document=passed.input_document,
        output=json.dumps(passed.final_explanation_json, ensure_ascii=False),
        expected_metadata=passed.expected_metadata(),
        canary=None,
    )
