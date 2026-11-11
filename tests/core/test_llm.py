import ast
import json
import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, SecretStr

from app.core import llm

"""
    공용 언어 모델 호출 (app/core/llm.py, GLOW-150)
      - 외부로 나가지 않는다. 이 PC 의 가짜 HTTP 서버(127.0.0.1)가 제공자 응답 모양으로 답하고, 실제 LiteLLM 이 그 서버를 부른다.
        그래서 "LiteLLM 이 실제로 몇 번 요청하는지·오류를 무엇으로 바꾸는지·무엇을 출력하는지"를 본다.
      1. 세 제공자(OpenAI·Claude·Gemini) 응답에서 글·토큰 수·모델 이름을 읽고, 요청은 한 번, 키는 헤더로만 간다
      2. 실패는 두 가지로만 올라온다: 시간 초과 / 그 밖(한도·인증·서버·거부·잘림·빈 응답·연결). 재시도는 없다(요청 1번)
      3. 키·프롬프트·제공자 원문은 예외 문구·로그·표준 출력 어디에도 없다
      4. LiteLLM 콜백(외부 기록)이 켜져 있으면 부르지 않는다. 가격표는 묶음에 든 것만 쓴다(원격 조회 없음)
      5. 사용량: 캐시에서 읽은 입력 토큰을 읽고, 거부·잘림·빈 응답의 토큰 수도 오류에 숫자로만 싣는다.
         제공자가 준 수·모델 이름은 그대로 믿지 않는다(범위를 벗어난 수 → 사용량 없음, 이상한 이름 → INVALID)
      6. LiteLLM 은 로더(load_llm_library) 안에서만 import 한다(다른 곳에서 import 하면 원격 가격표 조회가 켜진다)
"""

API_KEY = "sk-test-KEEP-THIS-SECRET-0123456789"
PROMPT = "PROMPT-MARKER 이 글은 JSON 으로 답하라는 프롬프트입니다"
ANSWER = '{"answer": "ok"}'

OPENAI = "openai/fake-model"
ANTHROPIC = "anthropic/claude-fake"
GEMINI = "gemini/gemini-fake"


def _openai_body(text=ANSWER, finish="stop"):
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "fake-model-2026-01-01",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def _anthropic_body(text=ANSWER, stop="end_turn"):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-fake-2026",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
        "usage": {"input_tokens": 13, "output_tokens": 5},
    }


def _gemini_body(text=ANSWER, finish="STOP"):
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": finish,
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 9,
            "candidatesTokenCount": 4,
            "totalTokenCount": 13,
        },
        "modelVersion": "gemini-fake-001",
    }


BODY = {OPENAI: _openai_body, ANTHROPIC: _anthropic_body, GEMINI: _gemini_body}
# 제공자마다 "잘림"·"거부"를 알리는 값
TRUNCATED = {OPENAI: "length", ANTHROPIC: "max_tokens", GEMINI: "MAX_TOKENS"}
REFUSED = {OPENAI: "content_filter", ANTHROPIC: "refusal", GEMINI: "SAFETY"}


class FakeProvider:
    """요청을 기록하고, 정해 둔 상태 코드·본문·지연으로 답하는 가짜 제공자"""

    def __init__(self):
        self.requests: list[SimpleNamespace] = []
        self.status = 200
        self.payload: dict = {}
        self.delay = 0.0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append(
                    SimpleNamespace(
                        path=self.path,
                        headers={k.lower(): v for k, v in self.headers.items()},
                        body=body,
                    )
                )
                if owner.delay:
                    time.sleep(owner.delay)
                raw = json.dumps(owner.payload).encode()
                try:
                    self.send_response(owner.status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass  # 시간 초과 시험: 호출 쪽이 먼저 끊음

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, payload, status=200, delay=0.0):
        self.requests.clear()
        self.payload, self.status, self.delay = payload, status, delay

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(scope="module")
def provider():
    fake = FakeProvider()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def point_providers_at_fake(provider, monkeypatch):
    """세 제공자의 주소를 가짜 서버로 돌린다. 실제 주소로는 한 번도 나가지 않는다."""
    monkeypatch.setenv("OPENAI_API_BASE", provider.url + "/v1")
    monkeypatch.setenv("ANTHROPIC_API_BASE", provider.url)
    monkeypatch.setenv("GEMINI_API_BASE", provider.url)


def _call(model=OPENAI, **overrides):
    params = {
        "model": model,
        "prompt": PROMPT,
        "api_key": SecretStr(API_KEY),
        "timeout_seconds": 5.0,
    }
    params.update(overrides)
    return llm.complete_text(**params)


def _error_body():
    """제공자가 오류 본문에 키와 프롬프트를 그대로 되돌려 주는 최악의 경우"""
    return {
        "error": {
            "message": f"bad request, key={API_KEY}, prompt={PROMPT}",
            "type": "error",
            "code": "error",
        }
    }


# --- 0. 설정 도우미 ---------------------------------------------------------------------------


def test_split_model_keeps_slashes_in_model_id():
    assert llm.split_model("openai/gpt-x") == ("openai", "gpt-x")
    assert llm.split_model("bedrock/anthropic.claude/v1") == (
        "bedrock",
        "anthropic.claude/v1",
    )


@pytest.mark.parametrize("value", ["", "openai", "/gpt-x", "openai/"])
def test_split_model_rejects_names_without_provider_or_model(value):
    with pytest.raises(llm.LlmConfigError):
        llm.split_model(value)


def test_api_key_is_one_per_provider():
    settings = SimpleNamespace(
        openai_api_key=SecretStr("o"),
        gemini_api_key=SecretStr("g"),
        anthropic_api_key=SecretStr("a"),
    )

    assert llm.api_key_for(settings, "openai").get_secret_value() == "o"
    assert llm.api_key_for(settings, "gemini").get_secret_value() == "g"
    assert llm.api_key_for(settings, "anthropic").get_secret_value() == "a"
    assert llm.api_key_for(settings, "bedrock") is None  # 실행 환경 자격 증명


def test_api_key_missing_or_unknown_provider_is_config_error():
    settings = SimpleNamespace(
        openai_api_key=None, gemini_api_key=None, anthropic_api_key=None
    )

    with pytest.raises(llm.LlmConfigError, match="OPENAI_API_KEY"):
        llm.api_key_for(settings, "openai")
    with pytest.raises(llm.LlmConfigError, match="모르는 모델 제공자"):
        llm.api_key_for(settings, "azure")


# --- 1. 세 제공자 정상 응답 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "tokens", "response_model"),
    [
        (OPENAI, (11, 7), "fake-model-2026-01-01"),
        (ANTHROPIC, (13, 5), "claude-fake-2026"),
        (GEMINI, (9, 4), "gemini-fake-001"),
    ],
)
def test_reads_text_usage_and_model_from_each_provider(
    provider, model, tokens, response_model
):
    provider.answer(BODY[model]())

    result = _call(model)

    assert result.text == ANSWER
    assert result.model == model
    assert result.response_model == response_model
    assert (result.usage.input_tokens, result.usage.output_tokens) == tokens
    assert result.elapsed_ms >= 0
    assert len(provider.requests) == 1


@pytest.mark.parametrize(
    ("model", "key_header", "expected"),
    [
        (OPENAI, "authorization", f"Bearer {API_KEY}"),
        (ANTHROPIC, "x-api-key", API_KEY),
        (GEMINI, "x-goog-api-key", API_KEY),
    ],
)
def test_key_goes_in_header_and_prompt_is_one_user_message(
    provider, model, key_header, expected
):
    provider.answer(BODY[model]())

    _call(model)

    request = provider.requests[0]
    assert request.headers[key_header] == expected
    assert API_KEY not in request.path  # 주소(로그에 남는 곳)에는 키가 없다
    sent = json.dumps(request.body, ensure_ascii=False)
    assert sent.count("PROMPT-MARKER") == 1
    assert '"system"' not in sent


def test_json_mode_is_sent_to_openai_and_gemini(provider):
    provider.answer(_openai_body())
    _call(OPENAI, response_format={"type": "json_object"})
    assert provider.requests[0].body["response_format"] == {"type": "json_object"}

    provider.answer(_gemini_body())
    _call(GEMINI, response_format={"type": "json_object"})
    config = provider.requests[0].body["generationConfig"]
    assert config["response_mime_type"] == "application/json"


def test_without_response_format_nothing_is_sent(provider):
    provider.answer(_openai_body())

    _call(OPENAI)

    assert "response_format" not in provider.requests[0].body


class Draft(BaseModel):
    answer: str


def test_pydantic_model_becomes_structured_output_request(provider):
    """다른 기능이 Pydantic 모델을 넘기면 제공자의 구조화 출력 요청이 되고, 결과 글을 그 모델로 읽을 수 있다."""
    provider.answer(_openai_body())

    result = _call(OPENAI, response_format=Draft)

    sent = provider.requests[0].body["response_format"]
    assert sent["type"] == "json_schema"
    assert "answer" in json.dumps(sent["json_schema"])
    assert Draft.model_validate_json(result.text) == Draft(answer="ok")


# --- 2. 실패는 두 가지, 재시도 없음 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (429, llm.REASON_RATE_LIMIT),
        (401, llm.REASON_AUTH),
        (403, llm.REASON_AUTH),
        (500, llm.REASON_SERVER),
        (503, llm.REASON_SERVER),
        (400, llm.REASON_BAD_REQUEST),
        (404, llm.REASON_BAD_REQUEST),
    ],
)
def test_http_errors_become_call_error_without_retry(provider, status, reason):
    provider.answer(_error_body(), status=status)

    with pytest.raises(llm.LlmCallError) as caught:
        _call(OPENAI)

    assert caught.value.reason == reason
    assert caught.value.status_code == status
    assert len(provider.requests) == 1  # 라이브러리가 다시 부르지 않는다


@pytest.mark.parametrize("model", [ANTHROPIC, GEMINI])
@pytest.mark.parametrize(
    ("status", "reason"),
    [(429, llm.REASON_RATE_LIMIT), (500, llm.REASON_SERVER), (401, llm.REASON_AUTH)],
)
def test_other_providers_errors_are_mapped_the_same(provider, model, status, reason):
    provider.answer(_error_body(), status=status)

    with pytest.raises(llm.LlmCallError) as caught:
        _call(model)

    assert caught.value.reason == reason
    assert len(provider.requests) == 1


@pytest.mark.parametrize("model", [OPENAI, ANTHROPIC, GEMINI])
def test_slow_answer_becomes_timeout_without_retry(provider, model):
    provider.answer(BODY[model](), delay=3.0)

    started = time.monotonic()
    with pytest.raises(llm.LlmTimeoutError) as caught:
        _call(model, timeout_seconds=0.4)

    assert caught.value.reason == llm.REASON_TIMEOUT
    assert time.monotonic() - started < 2.5  # 답(3초 뒤)을 끝까지 기다리지 않는다
    assert len(provider.requests) == 1
    assert not isinstance(caught.value, llm.LlmCallError)


def test_refused_connection_becomes_call_error(monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    monkeypatch.setenv("OPENAI_API_BASE", f"http://127.0.0.1:{closed_port}/v1")

    with pytest.raises(llm.LlmCallError) as caught:
        _call(OPENAI)

    assert caught.value.reason == llm.REASON_CONNECTION


@pytest.mark.parametrize("model", [OPENAI, ANTHROPIC, GEMINI])
def test_truncated_answer_is_a_call_error(provider, model):
    provider.answer(BODY[model]('{"answer": "o', TRUNCATED[model]))

    with pytest.raises(llm.LlmCallError) as caught:
        _call(model)

    assert caught.value.reason == llm.REASON_TRUNCATED


@pytest.mark.parametrize("model", [OPENAI, ANTHROPIC, GEMINI])
def test_refused_answer_is_a_call_error(provider, model):
    provider.answer(BODY[model]("도와드릴 수 없습니다", REFUSED[model]))

    with pytest.raises(llm.LlmCallError) as caught:
        _call(model)

    assert caught.value.reason == llm.REASON_REFUSAL


@pytest.mark.parametrize("text", ["", "   \n"])
def test_empty_answer_is_a_call_error(provider, text):
    provider.answer(_openai_body(text))

    with pytest.raises(llm.LlmCallError) as caught:
        _call(OPENAI)

    assert caught.value.reason == llm.REASON_EMPTY


def test_timeout_must_be_positive(provider):
    provider.answer(_openai_body())

    with pytest.raises(ValueError, match="timeout_seconds"):
        _call(OPENAI, timeout_seconds=0)
    assert provider.requests == []


# --- 3. 키·프롬프트·제공자 원문이 새지 않음 ------------------------------------------------------


@pytest.mark.parametrize("model", [OPENAI, ANTHROPIC, GEMINI])
@pytest.mark.parametrize("status", [401, 500])
def test_error_keeps_no_key_prompt_or_provider_text(
    provider, model, status, caplog, capfd
):
    provider.answer(_error_body(), status=status)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(llm.LlmError) as caught:
        _call(model)

    error = caught.value
    printed = capfd.readouterr()
    everything = "\n".join(
        [str(error), repr(error), caplog.text, printed.out, printed.err]
    )
    assert API_KEY not in everything
    assert "PROMPT-MARKER" not in everything
    assert "bad request" not in everything  # 제공자 원문
    # 원래 예외(제공자 원문이 든 것)가 새 예외에 달려 있지 않다
    assert error.__cause__ is None
    assert error.__context__ is None
    assert error.args == (
        f"모델 호출 실패 {error.reason} (모델 {model}, 상태 {status})",
    )


def test_success_logs_no_key_prompt_or_answer(provider, caplog, capfd):
    provider.answer(_openai_body())
    caplog.set_level(logging.DEBUG)

    _call(OPENAI)

    printed = capfd.readouterr()
    everything = f"{caplog.text}\n{printed.out}\n{printed.err}"
    assert API_KEY not in everything
    assert "PROMPT-MARKER" not in everything
    assert ANSWER not in everything


# --- 4. 외부 기록·원격 조회 없음 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "callback_list", ["callbacks", "success_callback", "failure_callback"]
)
def test_does_not_call_when_library_callbacks_are_on(
    provider, monkeypatch, callback_list
):
    """LiteLLM 콜백은 프롬프트·응답을 외부 기록 서비스로 보낸다(승인 없는 외부 전송)."""
    provider.answer(_openai_body())
    library = llm.load_llm_library()
    monkeypatch.setattr(library, callback_list, ["langsmith"])

    with pytest.raises(llm.LlmConfigError, match="콜백"):
        _call(OPENAI)

    assert provider.requests == []


def test_library_uses_bundled_price_table_and_stays_quiet():
    """LiteLLM 은 import 때 가격표를 원격에서 받아 온다. 우리 로더로 불러오면 묶음에 든 표만 쓴다."""
    library = llm.load_llm_library()
    from litellm.litellm_core_utils import get_model_cost_map

    info = get_model_cost_map._cost_map_source_info
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert info.source == "local"
    assert info.is_env_forced is True
    assert library.suppress_debug_info is True
    assert logging.getLogger("LiteLLM").getEffectiveLevel() >= logging.WARNING


def test_library_is_loaded_once():
    assert llm.load_llm_library() is llm.load_llm_library()


# --- 5. 사용량: 캐시 토큰·실패 응답·믿을 수 없는 값 -------------------------------------------


def _cached_body(model):
    """제공자마다 "캐시에서 읽은 입력 토큰"을 알리는 모양. 입력 1000 가운데 800이 캐시."""
    if model == OPENAI:
        body = _openai_body()
        body["usage"] = {
            "prompt_tokens": 1000,
            "completion_tokens": 7,
            "total_tokens": 1007,
            "prompt_tokens_details": {"cached_tokens": 800},
        }
    elif model == ANTHROPIC:
        body = _anthropic_body()
        body["usage"] = {
            "input_tokens": 200,  # Claude 는 캐시에서 읽은 수를 입력 수와 따로 준다
            "output_tokens": 7,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 0,
        }
    else:
        body = _gemini_body()
        body["usageMetadata"] = {
            "promptTokenCount": 1000,
            "candidatesTokenCount": 7,
            "totalTokenCount": 1007,
            "cachedContentTokenCount": 800,
        }
    return body


@pytest.mark.parametrize("model", [OPENAI, ANTHROPIC, GEMINI])
def test_reads_cached_input_tokens_from_each_provider(provider, model):
    provider.answer(_cached_body(model))

    usage = _call(model).usage

    # 캐시에서 읽은 수는 입력 수 안에 들어 있다(세 제공자 같은 뜻으로 맞춰진다)
    assert (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens) == (
        1000,
        800,
        7,
    )


def test_cached_input_tokens_are_none_when_provider_does_not_report(provider):
    provider.answer(_openai_body())

    assert _call(OPENAI).usage == llm.LlmUsage(input_tokens=11, output_tokens=7)


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": -5, "completion_tokens": 7},
        {"prompt_tokens": 11, "completion_tokens": -1},
        {"prompt_tokens": llm.MAX_TOKEN_COUNT + 1, "completion_tokens": 7},
        {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 12},  # 캐시 > 입력
        },
        {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": -7},
        },
    ],
)
def test_untrusted_token_counts_become_no_usage(provider, usage):
    body = _openai_body()
    body["usage"] = {**usage, "total_tokens": 1}
    provider.answer(body)

    result = _call(OPENAI)

    assert result.text == ANSWER  # 답은 그대로 쓴다. 사용량만 버린다
    assert result.usage == llm.LlmUsage(input_tokens=None, output_tokens=None)


@pytest.mark.parametrize(
    ("finish", "text", "reason"),
    [
        ("length", 'SECRET-ANSWER {"answer": "o', llm.REASON_TRUNCATED),
        ("content_filter", "SECRET-ANSWER 도와드릴 수 없습니다", llm.REASON_REFUSAL),
        ("stop", "   ", llm.REASON_EMPTY),
    ],
)
def test_billed_failures_carry_token_counts_only(provider, finish, text, reason):
    """거부·잘림·빈 응답은 실패지만 제공자가 청구한다. 오류는 토큰 수만 들고, 응답 글은 들지 않는다."""
    provider.answer(_openai_body(text, finish))

    with pytest.raises(llm.LlmCallError) as caught:
        _call(OPENAI)

    error = caught.value
    assert error.reason == reason
    assert error.usage == llm.LlmUsage(input_tokens=11, output_tokens=7)
    assert "SECRET-ANSWER" not in repr(vars(error)) + str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_failures_without_an_answer_have_no_usage(provider):
    provider.answer(_error_body(), status=429)

    with pytest.raises(llm.LlmCallError) as caught:
        _call(OPENAI)

    assert caught.value.usage is None


FORGED_LINE = "gpt-x" + chr(10) + "2026-11-12 00:00:00 INFO forged line"


@pytest.mark.parametrize(
    "name",
    [FORGED_LINE, "gpt x", "모델", "a" * 81, "gpt-x" + chr(13), "gpt-x" + chr(9)],
)
def test_strange_response_model_name_is_replaced(provider, name):
    body = _openai_body()
    body["model"] = name
    provider.answer(body)

    assert _call(OPENAI).response_model == llm.INVALID_MODEL_NAME


@pytest.mark.parametrize(
    "name",
    ["gpt-5.4-mini-2026-03-17", "anthropic.claude-3:0", "models/gemini_1.5", "a" * 80],
)
def test_safe_model_name_keeps_ordinary_names(name):
    assert llm.safe_model_name(name) == name


@pytest.mark.parametrize("value", [None, "", 7, "a b", FORGED_LINE, "a" * 81, "한글"])
def test_safe_model_name_rejects_everything_else(value):
    assert llm.safe_model_name(value) == llm.INVALID_MODEL_NAME


# --- 6. LiteLLM import 는 로더 안에서만 ------------------------------------------------------

APP_DIR = Path(__file__).resolve().parents[2] / "app"
LOADER = ("core/llm.py", "load_llm_library")


def _litellm_imports(tree: ast.AST, inside: str | None = None):
    """(가져오는 모듈 이름, 그 import 가 든 함수 이름) 목록"""
    for node in ast.iter_child_nodes(tree):
        scope = (
            node.name
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            else inside
        )
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            names = []
        for name in names:
            if name.split(".")[0] == "litellm":
                yield name, inside
        yield from _litellm_imports(node, scope)


def test_model_library_is_imported_only_inside_the_loader():
    """로더 밖에서 import 하면 가격표 원격 조회를 끄는 설정 없이 불려, 서버가 뜰 때 외부 주소로 나간다."""
    found = []
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative = path.relative_to(APP_DIR).as_posix()
        found += [(relative, scope) for _, scope in _litellm_imports(tree)]

    assert found == [LOADER]


def test_import_scan_catches_a_direct_import():
    """위 검사가 실제로 잡는지: 파일 맨 위·다른 함수 안·from 형태를 모두 찾아낸다."""
    code = chr(10).join(
        [
            "import litellm",
            "from litellm.utils import x",
            "def other():",
            "    import litellm.cost",
            "def load_llm_library():",
            "    import litellm",
            "import litellmx",
        ]
    )

    found = [scope for _, scope in _litellm_imports(ast.parse(code))]

    assert found == [None, None, "other", "load_llm_library"]
