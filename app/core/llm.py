import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, SecretStr

if TYPE_CHECKING:
    from app.core.config import Settings

"""
    공용 언어 모델 호출 (GLOW-150, docs/agent-flow/explanation.md 8절·9절)
      - 제공자(OpenAI·Gemini·Claude·Bedrock) 호출은 LiteLLM 한 겹으로 하고, LiteLLM 은 이 파일에서만 부른다.
        한 제공자에서 문제가 생기면 이 파일 안에서 그 제공자만 다른 방식으로 바꿀 수 있게 하려는 것이다.
      - LiteLLM 은 처음 호출할 때 불러온다. 모델을 쓰지 않는 서버(기본 mock)는 LiteLLM 을 메모리에 올리지 않는다.
      - 호출은 동기다. 부르는 쪽이 스레드에서 부른다(AI 설명은 엔진 생성기가 asyncio.to_thread 안에서 부름).
      - 재시도는 하지 않는다. 다시 부를지는 부르는 쪽이 정한다(AI 설명은 엔진의 3시도).
      - 실패는 두 가지로만 올린다: LlmTimeoutError(시간 초과), LlmCallError(그 밖의 모든 것).
        제공자 오류 문구에는 키·프롬프트 일부가 섞일 수 있어 버리고, 사유 코드·상태 코드만 남긴다.
      - 키·프롬프트·모델 출력은 로그에 남기지 않는다. LiteLLM 의 콜백(외부 기록)이 켜져 있으면 호출하지 않는다.
      - 키는 제공자마다 하나(설정의 SecretStr)이고 호출할 때 직접 넘긴다. 모델은 기능마다 자기 설정으로 고른다.
      - 사용량(토큰 수)은 제공자가 준 값이라 그대로 믿지 않는다. 범위를 벗어난 수가 하나라도 있으면 그 호출은 "사용량 없음"이다.
        제공자가 답한 모델 이름도 허용한 글자만 받는다(로그·추적 기록에 그대로 쓰이는 값). 합계·예상 비용은 app/core/llm_usage.py.
"""

logger = logging.getLogger(__name__)

# 제공자 접두어 → 설정의 키 칸. LiteLLM 의 제공자 이름과 같다("제공자/모델 ID")
API_KEY_FIELDS = {
    "openai": "openai_api_key",
    "gemini": "gemini_api_key",
    "anthropic": "anthropic_api_key",
}
# 키 대신 실행 환경의 자격 증명(인스턴스 역할 등)을 쓰는 제공자
KEYLESS_PROVIDERS = frozenset({"bedrock"})

# 호출 실패 사유 코드 (LlmCallError.reason)
REASON_TIMEOUT = "TIMEOUT"
REASON_AUTH = "AUTH"
REASON_RATE_LIMIT = "RATE_LIMIT"
REASON_REFUSAL = "REFUSAL"
REASON_CONNECTION = "CONNECTION"
REASON_SERVER = "SERVER"
REASON_BAD_REQUEST = "BAD_REQUEST"
REASON_TRUNCATED = "TRUNCATED"
REASON_EMPTY = "EMPTY"
REASON_UNEXPECTED = "UNEXPECTED"

# 토큰 수 상한. 이보다 크면 제공자 값이 잘못된 것으로 보고 버린다(가장 큰 문맥 창의 수백 배)
MAX_TOKEN_COUNT = 1_000_000_000
# 기록에 남겨도 되는 모델 이름: 영문·숫자와 . _ : / - 만, 80자까지. 줄바꿈·공백이 든 이름은 로그 줄을 위조할 수 있다
INVALID_MODEL_NAME = "INVALID"
_MODEL_NAME = re.compile(r"[A-Za-z0-9._:/-]{1,80}")


class LlmConfigError(RuntimeError):
    """모델을 부를 수 없는 설정(모르는 제공자, 키 없음, 라이브러리 없음, 외부 기록 콜백 켜짐). 호출 실패가 아니다."""


@dataclass(frozen=True)
class LlmUsage:
    """제공자가 알려 준 토큰 수. 알려 주지 않았거나 값이 범위를 벗어나면 None"""

    input_tokens: int | None
    output_tokens: int | None
    # 입력 토큰 가운데 제공자 캐시에서 읽은 수(할인 단가). input_tokens 에 들어 있는 수다
    cached_input_tokens: int | None = None


class LlmError(Exception):
    """모델 호출 실패. 문구에는 사유 코드·모델 이름·상태 코드만 있다(제공자 원문·키·프롬프트 없음).

    usage: 응답은 받았지만 결과로 쓰지 못한 호출(거부·잘림·빈 응답)의 토큰 수. 제공자가 청구하는 호출이라 남긴다.
    숫자만 들고 간다(응답 객체·글은 붙이지 않는다). 응답을 받지 못한 실패는 None.
    """

    def __init__(
        self,
        reason: str,
        *,
        model: str,
        status_code: int | None = None,
        usage: LlmUsage | None = None,
    ):
        super().__init__(f"모델 호출 실패 {reason} (모델 {model}, 상태 {status_code})")
        self.reason = reason
        self.model = model
        self.status_code = status_code
        self.usage = usage


class LlmTimeoutError(LlmError):
    """호출 시간 제한을 넘김"""


class LlmCallError(LlmError):
    """시간 초과가 아닌 호출 실패(연결·인증·한도·서버 오류·거부·잘린 응답·빈 응답 등)"""


@dataclass(frozen=True)
class LlmResult:
    text: str  # 모델이 쓴 글 전체(잘리거나 거부된 응답은 결과가 아니라 LlmCallError)
    model: str  # 요청한 "제공자/모델 ID"
    # 제공자가 답한 모델 이름(날짜가 붙은 판 이름일 수 있음). 허용한 글자가 아니면 "INVALID"
    response_model: str | None
    usage: LlmUsage
    elapsed_ms: int


def split_model(model: str) -> tuple[str, str]:
    """ "제공자/모델 ID" → (제공자, 모델 ID). 모델 ID 안의 "/" 는 그대로 둔다."""
    provider, _, model_id = model.partition("/")
    if not provider or not model_id:
        raise LlmConfigError(f'모델은 "제공자/모델 ID" 여야 합니다 (받은 값: {model})')
    return provider, model_id


def api_key_for(settings: Settings, provider: str) -> SecretStr | None:
    """그 제공자의 키. 키를 쓰지 않는 제공자는 None. 모르는 제공자거나 키가 비어 있으면 LlmConfigError."""
    if provider in KEYLESS_PROVIDERS:
        return None
    field = API_KEY_FIELDS.get(provider)
    if field is None:
        known = ", ".join(sorted({*API_KEY_FIELDS, *KEYLESS_PROVIDERS}))
        raise LlmConfigError(
            f"모르는 모델 제공자입니다: {provider} (쓸 수 있는 값: {known})"
        )
    api_key = getattr(settings, field)
    if api_key is None:
        raise LlmConfigError(
            f"{field.upper()} 가 없어 {provider} 모델을 부를 수 없습니다"
        )
    return api_key


_load_lock = threading.Lock()
_litellm_module: Any = None


def load_llm_library() -> Any:
    """LiteLLM 을 처음 한 번 불러와 설정한다. 설치돼 있지 않으면 LlmConfigError."""
    global _litellm_module
    if _litellm_module is None:
        with _load_lock:
            if _litellm_module is None:
                # LiteLLM 은 import 할 때 가격표를 원격에서 받아 온다(5초 제한). 이 값이 있어야 묶음에 든 표만 쓴다.
                # 외부로 나갈 길이 없는 서버에서 시작이 늦어지지 않게, 그리고 시작 때 외부 조회를 하지 않게 끈다.
                os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
                try:
                    import litellm
                except ImportError as error:
                    raise LlmConfigError(
                        "모델 호출 라이브러리(litellm)가 설치돼 있지 않습니다"
                    ) from error
                # 오류 때 표준 출력으로 내는 안내문을 끈다
                litellm.suppress_debug_info = True
                # LiteLLM·OpenAI SDK 의 DEBUG·INFO 로그에는 요청·응답 내용(프롬프트 포함)이 들어간다. 서버 로그 수준을
                # DEBUG 로 내려도 따라 내려가지 않게 수준을 직접 고정한다(로거에 수준이 없으면 루트 수준을 따른다).
                for name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "openai"):
                    library_logger = logging.getLogger(name)
                    if library_logger.level < logging.WARNING:
                        library_logger.setLevel(logging.WARNING)
                _litellm_module = litellm
    return _litellm_module


# LiteLLM 이 내부 캐시 객체를 만들 때 스스로 넣는 콜백 이름. 외부로 보내는 것이 아니다(응답 캐싱은 litellm.cache 가 있어야 켜짐)
_INTERNAL_CALLBACKS = frozenset({"cache"})


def _external_logging_enabled(litellm: Any) -> bool:
    """LiteLLM 콜백은 요청·응답을 외부 서비스로 보낸다. 하나라도 켜져 있으면 프롬프트가 승인 없이 나간다."""
    names = (
        "callbacks",
        "input_callback",
        "success_callback",
        "failure_callback",
        "service_callback",
        "_async_success_callback",
        "_async_failure_callback",
    )
    for name in names:
        for callback in getattr(litellm, name, None) or ():
            if not (isinstance(callback, str) and callback in _INTERNAL_CALLBACKS):
                return True
    return False


def _error_chain(error: BaseException) -> list[BaseException]:
    """예외와 그 원인들. LiteLLM 은 제공자 SDK·HTTP 예외를 자기 예외로 감싸므로 원래 종류는 사슬 안에 있다."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _is_timeout(litellm: Any, error: Exception) -> bool:
    import httpx
    import openai

    timeouts = (
        litellm.Timeout,
        openai.APITimeoutError,
        httpx.TimeoutException,
        TimeoutError,
    )
    return any(isinstance(item, timeouts) for item in _error_chain(error))


def _is_connection_failure(error: Exception) -> bool:
    import httpx
    import openai

    failures = (openai.APIConnectionError, httpx.TransportError, ConnectionError)
    return any(isinstance(item, failures) for item in _error_chain(error))


def _status_code(error: Exception) -> int | None:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _call_failure_reason(litellm: Any, error: Exception) -> str:
    """시간 초과가 아닌 실패의 사유 코드. 문구는 읽지 않고 예외 종류와 상태 코드만 본다."""
    status = _status_code(error)
    # 구체적인 것부터 본다(ContentPolicyViolationError 는 BadRequestError 의 자식)
    if isinstance(error, litellm.ContentPolicyViolationError):
        return REASON_REFUSAL
    if status in (401, 403) or isinstance(
        error, (litellm.AuthenticationError, litellm.PermissionDeniedError)
    ):
        return REASON_AUTH
    if status == 429 or isinstance(error, litellm.RateLimitError):
        return REASON_RATE_LIMIT
    # LiteLLM 은 연결 실패를 500 서버 오류로 감싸 올리기도 한다. 응답을 받지 못한 것이므로 서버 오류보다 먼저 본다
    if _is_connection_failure(error):
        return REASON_CONNECTION
    if isinstance(
        error,
        (
            litellm.InternalServerError,
            litellm.ServiceUnavailableError,
            litellm.BadGatewayError,
        ),
    ) or (status is not None and status >= 500):
        return REASON_SERVER
    if isinstance(error, litellm.BadRequestError) or (
        status is not None and 400 <= status < 500
    ):
        return REASON_BAD_REQUEST
    return REASON_UNEXPECTED


def complete_text(
    *,
    model: str,
    prompt: str,
    api_key: SecretStr | None,
    timeout_seconds: float,
    response_format: dict[str, Any] | type[BaseModel] | None = None,
) -> LlmResult:
    """
    모델을 한 번 부르고 글을 돌려준다. model 은 "제공자/모델 ID", prompt 는 사용자 메시지 하나로 보낸다.
    response_format 은 JSON 방식({"type": "json_object"})·JSON 스키마·Pydantic 모델 가운데 하나다.
    결과는 글(JSON 문자열일 수 있음)이며 Pydantic 모델로 읽는 것은 부르는 쪽이 한다(Model.model_validate_json).
    시간 제한은 HTTP 호출에 건다. 응답이 오지 않는 시간을 재는 것이라, 조금씩 흘려 보내는 응답은 더 걸릴 수 있다.
    """
    split_model(model)
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 는 0보다 커야 합니다")
    litellm = load_llm_library()
    if _external_logging_enabled(litellm):
        raise LlmConfigError(
            "LiteLLM 콜백이 켜져 있어 모델을 부르지 않습니다(프롬프트가 외부 기록 서비스로 나갑니다)"
        )

    params: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "timeout": timeout_seconds,
        "num_retries": 0,
        "max_retries": 0,
    }
    if api_key is not None:
        params["api_key"] = api_key.get_secret_value()
    if response_format is not None:
        params["response_format"] = response_format

    failure: LlmError | None = None
    response: Any = None
    started = time.monotonic()
    try:
        response = litellm.completion(**params)
    except Exception as error:  # noqa: BLE001 - 어떤 실패든 두 가지 호출 오류로 바꾼다(엔진은 그 둘만 시도로 셈)
        # 원래 예외는 문구에 제공자 응답 원문이 들어 있어 버린다. 아래에서 except 밖으로 나가 새 예외만 올린다.
        if _is_timeout(litellm, error):
            failure = LlmTimeoutError(REASON_TIMEOUT, model=model)
        else:
            reason = _call_failure_reason(litellm, error)
            failure = LlmCallError(reason, model=model, status_code=_status_code(error))
            if reason == REASON_UNEXPECTED:
                logger.error(
                    "모델 호출에서 분류하지 못한 예외: %s (모델 %s)",
                    type(error).__name__,
                    model,
                )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    if failure is None:
        failure, text = _read_text(response, model)
        if failure is not None:
            # 거부·잘림·빈 응답도 제공자가 청구한다
            failure.usage = _read_usage(response)
    if failure is not None:
        logger.warning(
            "모델 호출 실패: %s (모델 %s, 상태 %s, %dms)",
            failure.reason,
            model,
            failure.status_code,
            elapsed_ms,
        )
        raise failure

    logger.info("모델 호출 완료: 모델 %s, %dms", model, elapsed_ms)
    response_model = getattr(response, "model", None)
    return LlmResult(
        text=text,
        model=model,
        response_model=safe_model_name(response_model) if response_model else None,
        usage=_read_usage(response),
        elapsed_ms=elapsed_ms,
    )


def _read_text(response: Any, model: str) -> tuple[LlmError | None, str]:
    """응답에서 글을 꺼낸다. 거부·잘림·빈 응답은 글이 있어도 실패로 본다(반쯤 쓴 글을 결과로 주지 않는다)."""
    try:
        choice = response.choices[0]
        finish_reason = choice.finish_reason
        message = choice.message
        text = message.content
        refusal = getattr(message, "refusal", None)
    except AttributeError, IndexError, TypeError:
        return LlmCallError(REASON_UNEXPECTED, model=model), ""
    if finish_reason == "content_filter" or refusal:
        return LlmCallError(REASON_REFUSAL, model=model), ""
    if finish_reason == "length":
        return LlmCallError(REASON_TRUNCATED, model=model), ""
    if not isinstance(text, str) or not text.strip():
        return LlmCallError(REASON_EMPTY, model=model), ""
    return None, text


def safe_model_name(value: Any) -> str:
    """기록(로그·추적)에 남길 모델 이름. 허용한 글자·길이가 아니면 "INVALID"."""
    if isinstance(value, str) and _MODEL_NAME.fullmatch(value):
        return value
    return INVALID_MODEL_NAME


_NO_USAGE = LlmUsage(input_tokens=None, output_tokens=None)


def _read_usage(response: Any) -> LlmUsage:
    """응답의 토큰 수. LiteLLM 은 세 제공자 모두 캐시에서 읽은 입력 토큰을 prompt_tokens_details.cached_tokens 에 둔다.

    준 값 가운데 하나라도 정수가 아니거나 범위를 벗어나면(음수, 상한 초과, 캐시 > 입력) 전부 버린다.
    일부만 믿으면 예상 비용이 음수이거나 엉뚱해진다.
    """
    usage = getattr(response, "usage", None)
    details = getattr(usage, "prompt_tokens_details", None)
    raw = (
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
        getattr(details, "cached_tokens", None),
    )
    if not all(value is None or _is_token_count(value) for value in raw):
        return _NO_USAGE
    input_tokens, output_tokens, cached = raw
    if cached is not None and (input_tokens is None or cached > input_tokens):
        return _NO_USAGE
    return LlmUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
    )


def _is_token_count(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= MAX_TOKEN_COUNT
    )
