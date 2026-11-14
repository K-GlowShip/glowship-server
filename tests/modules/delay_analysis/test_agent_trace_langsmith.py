import json
import logging
import threading
import time
import uuid
from concurrent.futures import wait as wait_futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import ClassVar

import pytest
from langsmith.run_trees import _create_current_dotted_order
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.modules.delay_analysis.application.agent.tracing import RunTracer
from app.modules.delay_analysis.application.run_start import app_runner as A
from app.modules.delay_analysis.infrastructure.agent_trace_langsmith import (
    ROOT_RUN_NAME,
    UNFINISHED,
    LangSmithTraceSink,
)
from app.modules.delay_analysis.infrastructure.agent_trace_outbox import (
    OUTBOX_DIR_NAME,
    TraceOutboxSink,
)
from tests.modules.delay_analysis.test_agent_tracing import _run, _state

"""
    LangSmith 추적 전송기 (docs/agent-flow/agent-tracing.md 3절, agentflow 8.1)
      - 그래프 실행 하나 → LangSmith 실행 나무 하나를 한 번에 보냄(맨 위 id = 분석 실행 ID, 다시 보내도 같은 id)
      - 가린 값만 나감(주문 코드·프롬프트 원문 없음)
      - 내부 로그 싱크와 함께 두면 사건은 내부 로그에 바로 다 남음(실행이 끝나기 전에도). 전송 실패는 전송기만 세고 분석은 그대로
      - 보내기는 전송기의 데몬 스레드 — 응답 없는 주소여도 추적기 닫기가 기다리지 않음(검토 11-13: 133초 멈춤)
      - SKU 경로 조각도 가림(사용자 결정 11-13)
      - 실제 langsmith.Client 를 로컬 가짜 서버에 붙여 보낸 본문과 오류 전달 방식을 확인(외부 네트워크 없음)
      - 설정: 켜려면 키 필수, 시험 환경에서는 꺼짐
"""


class FakeClient:
    def __init__(self, error: Exception | None = None):
        self.calls: list[list[dict]] = []
        self.error = error

    def batch_ingest_runs(self, *, create):
        self.calls.append(list(create))
        if self.error is not None:
            raise self.error


def _close(tracer, sink, timeout=5.0):
    """추적기를 닫고(끝 사건 없는 실행도 넘김) 전송기 데몬 스레드가 다 보낼 때까지 기다린다."""
    assert tracer.close()
    assert sink.flush(timeout)


def _outbox_lines(base):
    files = list((base / OUTBOX_DIR_NAME).rglob("*.jsonl"))
    return [
        json.loads(line)
        for f in files
        for line in f.read_text(encoding="utf-8").splitlines()
    ]


def _by_id(runs):
    return {run["id"]: run for run in runs}


def _assert_tree(runs, trace_id):
    root = runs[0]
    assert root["id"] == trace_id and root["parent_run_id"] is None
    assert root["dotted_order"] == _create_current_dotted_order(
        root["start_time"], trace_id
    )
    by_id = _by_id(runs)
    assert len(by_id) == len(runs)  # id 가 겹치지 않음
    for run in runs[1:]:
        parent = by_id[run["parent_run_id"]]
        assert run["trace_id"] == trace_id
        assert run["dotted_order"] == (
            f"{parent['dotted_order']}.{_create_current_dotted_order(run['start_time'], run['id'])}"
        )
        assert parent["start_time"] <= run["start_time"] <= run["end_time"]


# --- 그래프 실행 하나 → 실행 나무 하나 -------------------------------------------------------


async def test_graph_run_is_sent_once_as_one_tree():
    state = _state()
    client = FakeClient()
    sink = LangSmithTraceSink(client, "proj")
    tracer = RunTracer(state["analysis_run_id"], state["company_id"], [sink])

    result = await _run(state, tracer)
    _close(tracer, sink)

    assert len(client.calls) == 1  # RUN_END 에서 한 번, 닫기에서는 보낼 것이 없음
    runs = client.calls[0]
    assert len(runs) == len(tracer.events) - sum(
        e.kind in ("NODE_END", "RUN_END") for e in tracer.events
    )  # 시작·끝 사건은 실행 하나로 합쳐짐
    _assert_tree(runs, state["analysis_run_id"])
    root = runs[0]
    assert root["name"] == ROOT_RUN_NAME and root["session_name"] == "proj"
    assert root["outputs"]["outcome"] == "READY_FOR_REVIEW" and root["error"] is None
    nodes = [r["name"] for r in runs if r["tags"] == ["NODE_START"]]
    assert nodes == result["visited_nodes"]
    assert all(r["end_time"] is not None and r["error"] is None for r in runs)
    for run in runs:
        meta = run["extra"]["metadata"]
        assert meta["analysis_run_id"] == str(state["analysis_run_id"])
        assert meta["company_id"] == str(state["company_id"])
    assert sink.sent_traces == 1 and sink.failed_traces == 0


async def test_sending_the_same_run_again_reuses_the_same_ids():
    run_id, company_id = uuid.uuid4(), uuid.uuid4()
    ids = []
    for _ in range(2):
        client = FakeClient()
        sink = LangSmithTraceSink(client, "p")
        tracer = RunTracer(run_id, company_id, [sink])
        await _run(_state(run_id, company_id), tracer)
        _close(tracer, sink)
        ids.append([r["id"] for r in client.calls[0]])
    assert ids[0] == ids[1]


def test_attempts_become_leaves_under_the_open_node():
    client = FakeClient()
    run_id = uuid.uuid4()
    sink = LangSmithTraceSink(client, "p")
    tracer = RunTracer(run_id, uuid.uuid4(), [sink])
    tracer.emit("RUN_START", "LangGraph", "STARTED")
    tracer.emit("NODE_START", "explain", "STARTED")
    tracer.tool_attempt(SimpleNamespace(tool_name="T9", attempt=1, error_code="TOOL_TIMEOUT",
                                        will_retry=True, elapsed_ms=5, step="S"))  # fmt: skip
    tracer.tool_attempt(SimpleNamespace(tool_name="T9", attempt=2, error_code=None,
                                        will_retry=False, elapsed_ms=3, step="S"))  # fmt: skip
    tracer.explanation_attempts(
        [{"retry_no": 1, "codes": [["SCHEMA_INVALID", "/x"]], "model_provider": "openai",
          "model_id": "m", "prompt_template_id": "p", "prompt_template_version": "1"}],
        usage=[{"input_tokens": 10, "output_tokens": 5}],
    )  # fmt: skip
    tracer.model_usage("explain", {"calls": 1, "input_tokens": 10})
    tracer.emit("NODE_END", "explain", "OK")
    tracer.emit("RUN_END", "LangGraph", "OK")
    _close(tracer, sink)

    runs = client.calls[0]
    _assert_tree(runs, run_id)
    node = next(
        r for r in runs if r["name"] == "explain" and r["tags"] == ["NODE_START"]
    )
    leaves = [r for r in runs if r["parent_run_id"] == node["id"]]
    assert [(r["run_type"], r["name"], r["error"]) for r in leaves] == [
        ("tool", "T9", "TOOL_TIMEOUT"),  # 재시도한 실패도 실패로 보임
        ("tool", "T9", None),
        ("llm", "explain", "SCHEMA_INVALID"),
        ("chain", "explain", None),
    ]
    assert leaves[2]["outputs"]["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert leaves[0]["extra"]["metadata"]["attempt"] == 1


def test_run_without_end_event_is_sent_when_the_tracer_closes():
    client = FakeClient()
    sink = LangSmithTraceSink(client, "p")
    tracer = RunTracer(uuid.uuid4(), uuid.uuid4(), [sink])
    tracer.emit("RUN_START", "LangGraph", "STARTED")
    tracer.emit("NODE_START", "prepare", "STARTED")
    assert sink.flush() and client.calls == []  # 끝 사건 전에는 보내지 않음

    _close(tracer, sink)

    runs = client.calls[0]
    assert [r["error"] for r in runs] == [UNFINISHED, UNFINISHED]
    assert all(r["end_time"] >= r["start_time"] for r in runs)
    tracer.close()  # 두 번 닫아도 다시 보내지 않음
    assert sink.flush() and len(client.calls) == 1


def test_payload_has_only_masked_values():
    client = FakeClient()
    sink = LangSmithTraceSink(client, "p")
    tracer = RunTracer(uuid.uuid4(), uuid.uuid4(), [sink])
    tracer.emit("RUN_START", "LangGraph", "STARTED",
                summary={"note": "A-ORD-20260701-0040이 늦음", "prompt_text": "규칙 1.", "api_key": "sk-x"})  # fmt: skip
    tracer.explanation_attempts(
        [{"retry_no": 1, "codes": [["SCHEMA_INVALID", "risk_summary:missing /sku/TONER20/increment_prepare_quantity"]],
          "model_provider": "openai", "model_id": "m", "prompt_template_id": "p", "prompt_template_version": "1"}]
    )  # fmt: skip
    tracer.emit("RUN_END", "LangGraph", "OK")
    _close(tracer, sink)

    dumped = json.dumps(client.calls, default=str, ensure_ascii=False)
    assert "A-ORD-20260701-0040" not in dumped and "ORD#" in dumped
    assert "규칙 1." not in dumped and "sk-x" not in dumped
    # 11-13 실측 실패 기록에 그대로 남던 SKU 코드(사용자 결정 11-13). 경로 모양은 남아 어디서 틀렸는지는 보임
    assert "TONER20" not in dumped and "missing /sku/SKU#" in dumped


# --- 전송 실패: 내부 로그에 남기고 분석은 그대로 ------------------------------------------------


async def test_events_reach_internal_log_before_the_run_ends(tmp_path):
    """서버가 실행 도중 죽어도 그때까지의 사건은 내부 로그에 있다(사용자 결정 11-13 — 전송기만 두면 메모리에만 있었음)."""
    client = FakeClient()
    sink = LangSmithTraceSink(client, "p")
    tracer = RunTracer(uuid.uuid4(), uuid.uuid4(), [TraceOutboxSink(tmp_path), sink])
    tracer.emit("RUN_START", "LangGraph", "STARTED")
    tracer.emit("NODE_START", "prepare", "STARTED")
    wait_futures(tracer._pending, timeout=5)  # 닫지 않음(= 끝 사건 전)

    assert [line["kind"] for line in _outbox_lines(tmp_path)] == [
        "RUN_START",
        "NODE_START",
    ]
    assert sink.flush() and client.calls == []
    _close(tracer, sink)


async def test_send_failure_is_counted_by_sender_and_analysis_is_unchanged(
    tmp_path, caplog
):
    state = _state()
    expected = await _run(dict(state))  # 같은 입력, 추적 없이
    sink = LangSmithTraceSink(FakeClient(ConnectionError("down")), "p")
    tracer = RunTracer(
        state["analysis_run_id"], state["company_id"], [TraceOutboxSink(tmp_path), sink]
    )

    with caplog.at_level(logging.WARNING):
        result = await _run(state, tracer)
        _close(tracer, sink)

    assert result == expected
    assert sink.failed_traces == 1 and sink.sent_traces == 0
    assert tracer.sink_failures == 0  # 내부 로그 쓰기는 성공, 전송 실패는 전송기 몫
    assert [line["seq"] for line in _outbox_lines(tmp_path)] == [
        e.seq for e in tracer.events
    ]
    assert (
        str(state["analysis_run_id"]) in caplog.text
        and "ConnectionError" in caplog.text
    )
    assert "down" not in caplog.text  # 오류 문장은 남기지 않음(종류만)


def test_client_built_from_settings_reports_errors_through_callback():
    """langsmith.Client 는 전송 오류를 올리지 않고 tracing_error_callback 으로만 알린다 — 그 길로도 실패를 잡는다."""
    seen = {}

    class CallbackClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.on_error = kwargs["tracing_error_callback"]

        def batch_ingest_runs(self, *, create):
            self.on_error(RuntimeError("401"))

    sink = LangSmithTraceSink.from_settings(
        api_key="k", project="p", endpoint=None, client_factory=CallbackClient
    )
    tracer = RunTracer(uuid.uuid4(), uuid.uuid4(), [sink])
    tracer.emit("RUN_START", "LangGraph", "STARTED")
    tracer.emit("RUN_END", "LangGraph", "OK")
    _close(tracer, sink)

    assert sink.failed_traces == 1
    assert seen["auto_batch_tracing"] is False  # 실패를 여기서 알려면 바로 보내야 함
    assert (
        seen["omit_traced_runtime_info"] is True
    )  # 호스트 등 실행 환경 정보는 보내지 않음
    assert seen["api_key"] == "k"
    assert (
        seen["retry_config"].total == 0
    )  # HTTP 단계 재시도 끔(응답 없는 주소에 오래 묶이지 않게)
    assert seen["timeout_ms"] == (2_000, 5_000)


# --- 실제 langsmith.Client (로컬 가짜 서버, 외부 네트워크 없음) ---------------------------------


class _Recorder(BaseHTTPRequestHandler):
    status = 200
    delay = 0.0
    bodies: ClassVar[list] = []

    def _reply(self, code, body=b"{}"):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # /info
        self._reply(200)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).bodies.append((self.path, self.headers.get("x-api-key"), body))
        if type(self).delay:
            time.sleep(type(self).delay)
        try:
            self._reply(type(self).status)
        except OSError:  # 클라이언트가 시간 제한으로 먼저 끊음
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_langsmith():
    handler = type("Handler", (_Recorder,), {"bodies": [], "status": 200, "delay": 0.0})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


async def test_real_client_posts_one_masked_batch(fake_langsmith):
    handler, url = fake_langsmith
    state = _state()
    sink = LangSmithTraceSink.from_settings(
        api_key="test-key", project="proj", endpoint=url
    )
    tracer = RunTracer(state["analysis_run_id"], state["company_id"], [sink])

    await _run(state, tracer)
    _close(tracer, sink, timeout=10)

    assert sink.sent_traces == 1 and sink.failed_traces == 0
    posts = [b for b in handler.bodies if b[0].endswith("/runs/batch")]
    assert len(posts) == 1
    _, api_key, body = posts[0]
    assert api_key == "test-key"
    sent = json.loads(body)["post"]
    assert {r["trace_id"] for r in sent} == {str(state["analysis_run_id"])}
    assert all(r["session_name"] == "proj" and r["dotted_order"] for r in sent)
    assert not any(
        "runtime" in (r.get("extra") or {}) for r in sent
    )  # 실행 환경 정보 없음


def test_real_client_server_error_is_counted_and_internal_log_has_all(
    fake_langsmith, tmp_path
):
    handler, url = fake_langsmith
    handler.status = 403
    sink = LangSmithTraceSink.from_settings(api_key="bad", project="p", endpoint=url)
    tracer = RunTracer(uuid.uuid4(), uuid.uuid4(), [TraceOutboxSink(tmp_path), sink])
    tracer.emit("RUN_START", "LangGraph", "STARTED")
    tracer.emit("RUN_END", "LangGraph", "OK")
    _close(tracer, sink, timeout=10)

    assert sink.failed_traces == 1
    assert len(_outbox_lines(tmp_path)) == 2


def test_unresponsive_server_does_not_block_closing(fake_langsmith):
    """검토 재현(11-13): 응답 없는 주소면 보내기 하나가 133초 걸리고 추적기 닫기·서버 종료가 그만큼 기다렸다."""
    handler, url = fake_langsmith
    handler.delay = 3.0  # 시간 제한(읽기 0.3초)보다 오래 답하지 않음
    sink = LangSmithTraceSink.from_settings(
        api_key="k", project="p", endpoint=url, timeout_ms=(200, 300)
    )
    tracer = RunTracer(uuid.uuid4(), uuid.uuid4(), [sink])
    tracer.emit("RUN_START", "LangGraph", "STARTED")
    tracer.emit("RUN_END", "LangGraph", "OK")

    started = time.monotonic()
    assert tracer.close(timeout=2)
    assert time.monotonic() - started < 1.0  # 닫기는 보내기를 기다리지 않음
    assert sink._sender.daemon  # 서버 종료도 기다리지 않음
    assert (
        sink.flush(timeout=20) and sink.failed_traces == 1
    )  # 재시도 없이 시간 제한으로 실패


# --- 설정 ---------------------------------------------------------------------------------


def _settings(**env):
    # app_env 를 직접 넣는다: APP_ENV 를 필수로 바꾸는 변경(#148) 전후 모두 이 설정이 만들어지게
    return Settings(
        _env_file=None,
        app_env="local",
        postgres_host="h",
        postgres_port=5432,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        jwt_secret_key="x" * 32,
        **env,
    )


def test_enabling_langsmith_without_key_fails_at_startup(monkeypatch):
    monkeypatch.delenv(
        "LANGSMITH_API_KEY", raising=False
    )  # 개발자 PC 환경 변수에 키가 있어도(검토 11-13)
    with pytest.raises(ValidationError, match="LANGSMITH_API_KEY"):
        _settings(agent_trace_langsmith_enabled=True)
    with pytest.raises(ValidationError, match="LANGSMITH_API_KEY"):
        _settings(agent_trace_langsmith_enabled=True, langsmith_api_key="  ")


def test_trace_sink_choice(tmp_path):
    """켜면 내부 로그 싱크 + 전송기를 함께(사용자 결정 11-13). 내부 로그가 먼저라 사건마다 파일에 바로 남는다."""

    def kinds(sinks):
        return [type(x).__name__ for x in sinks]

    assert A.build_trace_sinks(_settings()) == []
    assert kinds(A.build_trace_sinks(_settings(agent_trace_dir=tmp_path))) == [
        "TraceOutboxSink"
    ]
    on = {"agent_trace_langsmith_enabled": True, "langsmith_api_key": "k"}
    assert kinds(A.build_trace_sinks(_settings(agent_trace_dir=tmp_path, **on))) == [
        "TraceOutboxSink",
        "LangSmithTraceSink",
    ]
    assert kinds(A.build_trace_sinks(_settings(**on))) == ["LangSmithTraceSink"]
    assert A.build_tracer_factory([]) is None


def test_langsmith_without_trace_dir_warns_at_startup(caplog, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "missing_engine_tools", list)
    settings = _settings(
        analysis_auto_start_enabled=True,
        glowship_engine_dir=tmp_path,
        agent_trace_langsmith_enabled=True,
        langsmith_api_key="k",
    )
    with caplog.at_level(logging.WARNING):
        A.check_auto_start_ready(settings, environ={})
    assert "내부 로그가 없습니다" in caplog.text


def test_test_environment_never_sends_to_langsmith():
    """개발자 .env 에서 켜 두어도 시험은 보내지 않는다(tests/conftest.py 가 끔)."""
    assert get_settings().agent_trace_langsmith_enabled is False
