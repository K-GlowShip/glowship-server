import logging
import time
from collections.abc import Callable
from typing import Any

from pydantic import SecretStr

from app.core.llm import (
    MODEL_CHECK_MISMATCH,
    REASON_MODEL_MISMATCH,
    LlmCallError,
    LlmError,
    LlmModelMismatchError,
    LlmResult,
    LlmTimeoutError,
    LlmUsage,
    complete_text,
    safe_model_name,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

"""
    AI 설명 모델 어댑터 (GLOW-150, docs/agent-flow/explanation.md 8절)
      - 엔진 생성기(explain.generator.generate)가 부르는 계약을 지킨다: 동기 complete(prompt, request) → JSON 원문,
        속성 provider·model_id. 엔진은 이 둘로 등록부의 모델 설정을 찾고, 저장할 모델 이름을 만든다.
      - 실제 호출은 공용 모델 호출 층(app/core/llm.py)이 한다. 프롬프트에 입력 문서가 이미 들어 있어(엔진 render_prompt)
        사용자 메시지 하나로 보낸다. request(입력 문서·canary)는 쓰지 않고 어디에도 남기지 않는다.
      - 엔진은 ModelTimeout·ModelCallError 만 실패한 시도로 센다. 그 밖의 예외는 생성기를 뚫고 나가 실행이 예기치 않은
        오류로 끝난다. 그래서 공용 층의 두 오류를 엔진에서 불러온 같은 클래스로 바꾼다.
      - 설명 한 건의 총 시간 예산: 첫 호출부터 재고, 남은 시간이 없으면 부르지 않고 ModelTimeout 을 올린다.
        호출 한 번의 시간 제한도 남은 예산을 넘지 않게 줄인다. 엔진 3시도의 최악 시간이 예산 안에 묶인다.
      - 어댑터는 설명 한 건마다 새로 만든다(selection.new_explanation_adapter). 예산·시도별 사용량이 건별 상태라서
        여러 실행이 하나를 같이 쓰면 값이 섞인다.
      - 시도별 사용량(attempt_usage): 엔진 시도 기록과 같은 순서. 토큰 수·걸린 시간, 실패면 사유 코드.
        거부·잘림·빈 응답은 실패지만 제공자가 청구하므로 토큰 수도 남긴다. 시간 예산이 떨어져 부르지 않은 시도는
        requested=False 로 표시한다(제공자에게 요청이 안 나감 → 호출 수·비용에 넣지 않음, app/core/llm_usage.py).
      - 모델 대조(설정한 모델만 부른다, docs/agent-flow/explanation.md 8.12절):
          · 부르기 전: 지금 부르려는 "제공자/모델 ID" 가 서버 설정(configured_model)과 다르면 부르지 않는다.
          · 부른 뒤: 답한 모델이 요청한 모델이 아니면 공용 층이 LlmModelMismatchError 를 올린다(app/core/llm.py).
        둘 다 엔진 오류로 바꾸지 않고 그대로 올린다. 엔진은 시도로 세지 않아 남은 시도를 하지 않고, 실행은 예상하지 못한
        오류로 끝난다. 시도별 사용량에는 답한 모델(response_model)과 대조 결과(model_check)를 남긴다.
"""

logger = logging.getLogger(__name__)

# 엔진 검증기가 JSON 원문을 읽으므로 제공자에게 JSON 방식으로 답하게 한다. 스키마 강제는 하지 않는다
# (explanation.v1 의 길이·패턴 제약을 제공자들이 그대로 받지 못함). 검증은 엔진이 한다.
EXPLANATION_RESPONSE_FORMAT = {"type": "json_object"}

BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


class ExplanationModelAdapter:
    def __init__(
        self,
        *,
        provider: str,
        model_id: str,
        api_key: SecretStr | None,
        call_timeout_seconds: float,
        total_budget_seconds: float,
        configured_model: str | None = None,
        complete: Callable[..., LlmResult] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.provider = provider
        self.model_id = model_id
        # 서버 설정의 "제공자/모델 ID". 넘기면 부를 때마다 지금 부르려는 모델과 대조한다(앱 조립이 넘긴다)
        self._configured_model = configured_model
        self.attempt_usage: list[dict[str, Any]] = []
        self._api_key = api_key
        self._call_timeout_seconds = call_timeout_seconds
        self._total_budget_seconds = total_budget_seconds
        # 넘기지 않으면 공용 모델 호출 층을 쓴다(시험에서만 바꿔 넣는다)
        self._complete = complete if complete is not None else complete_text
        self._clock = clock
        self._deadline: float | None = None

    def complete(self, prompt: str, request: Any) -> str:
        engine_errors = load_engine_module("explain.adapter")
        model = f"{self.provider}/{self.model_id}"
        if self._configured_model is not None and model != self._configured_model:
            self.attempt_usage.append(
                {
                    "error": REASON_MODEL_MISMATCH,
                    "elapsed_ms": 0,
                    "requested": False,
                    "response_model": safe_model_name(model),
                    "model_check": MODEL_CHECK_MISMATCH,
                }
            )
            logger.critical(
                "설정한 설명 모델과 다른 모델을 부르려 해 멈춥니다: 설정 %s, 부르려던 모델 %s",
                safe_model_name(self._configured_model),
                safe_model_name(model),
            )
            raise LlmModelMismatchError(
                model=self._configured_model,
                response_model=safe_model_name(model),
                called=False,
            )
        now = self._clock()
        if self._deadline is None:
            self._deadline = now + self._total_budget_seconds
        remaining = self._deadline - now
        if remaining <= 0:
            self.attempt_usage.append(
                {"error": BUDGET_EXHAUSTED, "elapsed_ms": 0, "requested": False}
            )
            raise engine_errors.ModelTimeout(BUDGET_EXHAUSTED)
        try:
            result = self._complete(
                model=model,
                prompt=prompt,
                api_key=self._api_key,
                timeout_seconds=min(self._call_timeout_seconds, remaining),
                response_format=EXPLANATION_RESPONSE_FORMAT,
            )
        except LlmTimeoutError as error:
            self.attempt_usage.append(self._failed(error, now))
            raise engine_errors.ModelTimeout(error.reason) from error
        except LlmCallError as error:
            self.attempt_usage.append(self._failed(error, now))
            raise engine_errors.ModelCallError(error.reason) from error
        except LlmModelMismatchError as error:
            # 엔진 오류로 바꾸지 않는다(바꾸면 엔진이 실패한 시도로 세고 다시 부른다). 누가 답했는지는 사용량에 남긴다
            record = self._failed(error, now)
            record["model_check"] = MODEL_CHECK_MISMATCH
            if error.response_model:
                record["response_model"] = error.response_model
            if not error.called:
                record["requested"] = False
            self.attempt_usage.append(record)
            raise
        record = {
            **_tokens(result.usage),
            "elapsed_ms": result.elapsed_ms,
            "model_check": result.model_check,
        }
        if result.response_model:
            record["response_model"] = result.response_model
        self.attempt_usage.append(record)
        return result.text

    def _failed(self, error: LlmError, started: float) -> dict[str, Any]:
        return {
            "error": error.reason,
            **(_tokens(error.usage) if error.usage is not None else {}),
            "elapsed_ms": int((self._clock() - started) * 1000),
        }


def _tokens(usage: LlmUsage) -> dict[str, Any]:
    """사용량에서 숫자 칸만. 캐시에서 읽은 입력 토큰은 제공자가 알려 준 때만 넣는다."""
    tokens: dict[str, Any] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
    }
    if usage.cached_input_tokens is not None:
        tokens["cached_input_tokens"] = usage.cached_input_tokens
    return tokens
