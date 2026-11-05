import asyncio
import json
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.delay_analysis.application.agent.graph import build_analysis_graph
from app.modules.delay_analysis.application.agent.retry import (
    TOOL_TEMPORARY_ERROR_EXHAUSTED,
    TransientToolError,
    call_with_retry,
)
from app.modules.delay_analysis.application.agent.state import AnalysisStep
from app.modules.delay_analysis.application.agent.tracing import (
    AUTO_TRACING_ENV,
    RunTracer,
    TraceConfigError,
    TraceMasker,
    auto_tracing_env_on,
    build_run_tracer,
)
from app.modules.delay_analysis.infrastructure.agent_trace_outbox import (
    OUTBOX_DIR_NAME,
    TraceOutboxSink,
)
from app.modules.delay_analysis.infrastructure.repository.run_status_repository import (
    AnalysisRunStatusReader,
)

"""
    에이전트 실행 추적 (docs/agent-flow/agent-tracing.md)
      - 그래프 뼈대(build_analysis_graph)를 그대로 돌려 실행 하나의 사건이 시작 → 노드·갈림길 → 종료로 이어지는지 본다
      - 추적 장애 독립성: 기록이 예외를 내거나 느려도 그래프 결과가 같고, 분석이 기다리지 않는다
      - 마스킹·금지 항목·기본 꺼짐(자동 추적 환경 변수)·내부 로그 파일
      - 통합: 실제 실행 상태 조회기(DB)로 STALE 실행을 돌려 노드 경계 중단이 추적에 남는지
"""


class MemorySink:
    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(event)


class FailingSink:
    def write(self, event):
        raise OSError("disk full")


class SlowSink:
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.events = []

    def write(self, event):
        time.sleep(self.seconds)
        self.events.append(event)


class _Status:
    def __init__(self, status: str):
        self.status = status

    async def get_status(self, analysis_run_id):
        return self.status


def _state(run_id=None, company_id=None):
    return {
        "analysis_run_id": run_id or uuid.uuid4(),
        "company_id": company_id or uuid.uuid4(),
        "delay_event_id": uuid.uuid4(),
        "operating_rule_set_id": uuid.uuid4(),
    }


async def _run(state, tracer=None, status="ANALYZING_IMPACT", reader=None):
    config = {"configurable": {"run_status_reader": reader or _Status(status)}}
    if tracer is not None:
        config["callbacks"] = [tracer.callback_handler()]
    return await build_analysis_graph().ainvoke(state, config=config)


def _kinds(events):
    return [(e.kind, e.name) for e in events]


# --- 실행 하나가 처음부터 끝까지 이어짐 ----------------------------------------------------


async def test_one_run_is_traced_from_start_to_end_state():
    state = _state()
    sink = MemorySink()
    tracer = RunTracer(state["analysis_run_id"], state["company_id"], [sink])

    result = await _run(state, tracer)
    assert tracer.close()

    nodes = [e.name for e in sink.events if e.kind == "NODE_END"]
    assert nodes == result["visited_nodes"]  # 그래프가 실제로 지난 노드 순서 그대로
    assert _kinds(sink.events)[0] == ("RUN_START", "LangGraph")
    assert sink.events[-1].kind == "RUN_END"
    assert sink.events[-1].summary["outcome"] == "READY_FOR_REVIEW"
    routes = [e.summary["next"] for e in sink.events if e.kind == "ROUTE"]
    assert routes == [
        "coordinator",
        "inventory_impact",
        "response_options",
        "compare",
        "validate",
    ]
    assert [e.seq for e in sink.events] == list(range(1, len(sink.events) + 1))
    assert {(e.analysis_run_id, e.company_id, e.trace_id) for e in sink.events} == {
        (state["analysis_run_id"], state["company_id"], state["analysis_run_id"])
    }
    assert all(
        e.elapsed_ms is not None
        for e in sink.events
        if e.kind in ("NODE_END", "RUN_END")
    )


async def test_event_ids_are_stable_for_the_same_run():
    # 다시 보내도 중복이 생기지 않게 사건 ID = uuid5(실행 ID, 순번)
    state = _state()
    first, second = MemorySink(), MemorySink()
    for sink in (first, second):
        tracer = RunTracer(state["analysis_run_id"], state["company_id"], [sink])
        await _run(state, tracer)
        tracer.close()

    assert [e.event_id for e in first.events] == [e.event_id for e in second.events]
    assert len({e.event_id for e in first.events}) == len(first.events)


async def test_stale_stop_and_node_error_are_traced():
    state = _state()
    sink = MemorySink()
    tracer = RunTracer(state["analysis_run_id"], state["company_id"], [sink])

    result = await _run(state, tracer, status="STALE")
    tracer.close()

    assert result["outcome"] == "STALE"
    coordinator_end = next(
        e for e in sink.events if e.kind == "NODE_END" and e.name == "coordinator"
    )
    assert coordinator_end.summary["outcome"] == "STALE"
    assert sink.events[-1].summary["outcome"] == "STALE"

    class Broken:
        async def get_status(self, analysis_run_id):
            raise RuntimeError("db down A-ORD-20260613-0040")

    error_sink = MemorySink()
    error_tracer = RunTracer(
        state["analysis_run_id"], state["company_id"], [error_sink]
    )
    with pytest.raises(RuntimeError):
        await _run(state, error_tracer, reader=Broken())
    error_tracer.close()
    kinds = [e.kind for e in error_sink.events]
    assert "NODE_ERROR" in kinds and kinds[-1] == "RUN_ERROR"
    assert "A-ORD-20260613-0040" not in json.dumps(
        [e.to_json_dict() for e in error_sink.events]
    )


# --- 재시도·설명 시도 연결 --------------------------------------------------------------


def test_tool_attempts_from_retry_hook():
    # retry.AttemptRecord(GLOW-123)와 같은 칸: tool_name·step·attempt·error_code·elapsed_ms·will_retry
    sink = MemorySink()
    tracer = RunTracer(uuid.uuid4(), uuid.uuid4(), [sink])
    records = [
        SimpleNamespace(tool_name="get_sales_history", step="ANALYZING_IMPACT", attempt=1,
                        error_code="TOOL_TIMEOUT", elapsed_ms=5000, will_retry=True),
        SimpleNamespace(tool_name="get_sales_history", step="ANALYZING_IMPACT", attempt=2,
                        error_code=None, elapsed_ms=120, will_retry=False),
    ]  # fmt: skip

    for record in records:
        tracer.tool_attempt(record)
    tracer.close()

    assert [(e.kind, e.attempt, e.status, e.error_code) for e in sink.events] == [
        ("TOOL_ATTEMPT", 1, "RETRY", "TOOL_TIMEOUT"),
        ("TOOL_ATTEMPT", 2, "OK", None),
    ]
    assert sink.events[0].summary == {"step": "ANALYZING_IMPACT", "will_retry": True}


async def test_real_call_with_retry_feeds_tool_attempts():
    # GLOW-123(PR #28 머지)의 실제 call_with_retry 에 on_attempt=tracer.tool_attempt 를 넘긴다
    sink = MemorySink()
    tracer = RunTracer(uuid.uuid4(), uuid.uuid4(), [sink])
    calls = {"n": 0}

    async def flaky_tool():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TransientToolError("원천 응답 지연 A-ORD-20260613-0040")
        return "ok"

    async def no_sleep(seconds):
        return None

    result = await call_with_retry(
        "get_sales_history",
        AnalysisStep.ANALYZING_IMPACT,
        flaky_tool,
        sleep=no_sleep,
        on_attempt=tracer.tool_attempt,
    )
    tracer.close()

    assert result == "ok"
    assert [(e.attempt, e.status, e.error_code) for e in sink.events] == [
        (1, "RETRY", TOOL_TEMPORARY_ERROR_EXHAUSTED),
        (2, "OK", None),
    ]
    assert sink.events[0].summary == {"step": "ANALYZING_IMPACT", "will_retry": True}


def test_explanation_attempts_keep_codes_but_not_prompt_or_output():
    sink = MemorySink()
    tracer = RunTracer(uuid.uuid4(), uuid.uuid4(), [sink])
    attempts = [
        {"retry_no": 1, "codes": [["FORBIDDEN_EXPRESSION", "/risk_summary/0"]], "model_provider": "mock",
         "model_id": "mock-template-v1", "prompt_template_id": "explain_ready_v1", "prompt_template_version": "1.0.0",
         "prompt_template_sha256": "abc", "prompt_text": "규칙 1. …", "raw_output": "{...}"},
        {"retry_no": 2, "codes": [], "model_provider": "mock", "model_id": "mock-template-v1",
         "prompt_template_id": "explain_ready_v1", "prompt_template_version": "1.0.0"},
    ]  # fmt: skip

    tracer.explanation_attempts(
        attempts, usage=[{"input_tokens": 900, "output_tokens": 300}, None]
    )
    tracer.close()

    assert [(e.attempt, e.status, e.error_code) for e in sink.events] == [
        (1, "ERROR", "FORBIDDEN_EXPRESSION"),
        (2, "OK", None),
    ]
    first = sink.events[0].summary
    assert first["codes"] == ["FORBIDDEN_EXPRESSION@/risk_summary/0"]
    assert first["prompt_version"] == "explain_ready_v1@1.0.0"
    assert first["usage"] == {"input_tokens": 900, "output_tokens": 300}
    dumped = json.dumps([e.to_json_dict() for e in sink.events], ensure_ascii=False)
    assert "규칙 1." not in dumped and "raw_output" not in dumped


# --- 추적 장애 독립성 -----------------------------------------------------------------


async def test_failing_sink_does_not_change_analysis():
    state = _state()
    expected = await _run(state)
    tracer = RunTracer(
        state["analysis_run_id"], state["company_id"], [FailingSink(), MemorySink()]
    )

    result = await _run(state, tracer)
    tracer.close()

    assert result == expected
    assert tracer.sink_failures == len(tracer.events) > 0


async def test_slow_sink_does_not_block_analysis():
    state = _state()
    expected = await _run(state)
    slow = SlowSink(0.2)
    tracer = RunTracer(state["analysis_run_id"], state["company_id"], [slow])

    started = time.perf_counter()
    result = await _run(state, tracer)
    elapsed = time.perf_counter() - started

    assert result == expected
    assert (
        elapsed < 0.2 * len(tracer.events) / 2
    )  # 사건마다 0.2초씩 기다렸다면 훨씬 오래 걸림
    assert (
        tracer.close(timeout=0.05) is False
    )  # 아직 쓰는 중이어도 분석 쪽은 기다리지 않음
    deadline = time.time() + 10
    while len(slow.events) < len(tracer.events) and time.time() < deadline:
        await asyncio.sleep(0.05)
    assert len(slow.events) == len(tracer.events)  # 나중에라도 다 씀


def test_emit_never_raises_even_if_masking_breaks(monkeypatch):
    tracer = RunTracer(uuid.uuid4(), uuid.uuid4(), [MemorySink()])
    monkeypatch.setattr(
        tracer.masker, "value", lambda value: (_ for _ in ()).throw(ValueError("boom"))
    )

    assert tracer.emit("RUN_START", "graph", "STARTED", summary={"a": 1}) is None
    assert tracer.sink_failures == 1
    tracer.close()


# --- 마스킹·금지 항목 -----------------------------------------------------------------


def test_masker_hides_order_codes_and_emails_consistently_within_a_run():
    masker = TraceMasker()
    other_run = TraceMasker()

    first = masker.text("주문 A-ORD-20260613-0040 지연, 담당 lead@a-beauty.example")
    again = masker.text("A-ORD-20260613-0040")

    assert "A-ORD-20260613-0040" not in first and "lead@a-beauty.example" not in first
    assert "[EMAIL]" in first
    assert again in first  # 같은 실행 안에서는 같은 가림 값
    assert (
        other_run.text("A-ORD-20260613-0040") != again
    )  # 실행이 다르면 다름(되돌리기 어려움)


def test_masker_removes_secret_and_prompt_keys_and_limits_size():
    masker = TraceMasker()

    summary = masker.value(
        {
            "password": "p",
            "api_key": "k",
            "access_token": "t",
            "input_tokens": 900,
            "prompt_version": "explain_ready_v1@1.0.0",
            "prompt_text": "규칙 전문",
            "input_document": {"run": "..."},
            "rows": list(range(30)),
            "note": "x" * 500,
        }
    )

    assert (
        summary["password"]
        == summary["api_key"]
        == summary["prompt_text"]
        == "[REMOVED]"
    )
    assert summary["access_token"] == "[REMOVED]"
    assert (
        summary["input_tokens"] == 900
        and summary["prompt_version"] == "explain_ready_v1@1.0.0"
    )  # 수·판 번호는 남김
    assert summary["input_document"] == "[REMOVED]"
    assert len(summary["rows"]) == 21 and summary["rows"][-1] == "…(+10)"
    assert len(summary["note"]) <= 201


def test_state_summary_keeps_keys_and_only_safe_values():
    masker = TraceMasker()

    summary = masker.state(
        {
            "step": "VALIDATING",
            "impact_result": {"orders": ["A-ORD-20260613-0040"]},
            "outcome": "NO_RISK",
        }
    )

    assert summary == {
        "keys": ["impact_result", "outcome", "step"],
        "outcome": "NO_RISK",
        "step": "VALIDATING",
    }


# --- 기본 꺼짐·내부 로그 ---------------------------------------------------------------


def test_auto_tracing_env_blocks_tracer():
    assert auto_tracing_env_on({}) == []
    for name in AUTO_TRACING_ENV:
        with pytest.raises(TraceConfigError):
            build_run_tracer(uuid.uuid4(), uuid.uuid4(), [], environ={name: "true"})
    assert auto_tracing_env_on({"LANGSMITH_TRACING": "false"}) == []
    tracer = build_run_tracer(uuid.uuid4(), uuid.uuid4(), [], environ={})
    assert isinstance(tracer, RunTracer)
    tracer.close()


def test_test_environment_has_no_auto_tracing():
    # 이 저장소의 시험·개발 환경에서 자동 추적이 켜져 있으면 안 됨(마스킹 없는 외부 전송)
    assert auto_tracing_env_on() == []


async def test_outbox_sink_writes_one_json_line_per_event(tmp_path):
    state = _state()
    sink = TraceOutboxSink(tmp_path)
    tracer = RunTracer(state["analysis_run_id"], state["company_id"], [sink])
    tracer.emit("ROUTE", "route_x", "OK", summary={"next": "A-ORD-20260613-0040"})
    await _run(state, tracer)
    assert tracer.close()

    files = list(
        (tmp_path / OUTBOX_DIR_NAME / str(state["company_id"])).glob("*.jsonl")
    )
    assert len(files) == 1 and files[0].name == f"{tracer.events[0].at[:10]}.jsonl"
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["seq"] for line in lines] == [e.seq for e in tracer.events]
    assert "A-ORD-20260613-0040" not in files[0].read_text(encoding="utf-8")


def test_outbox_sink_is_safe_for_concurrent_runs(tmp_path):
    sink = TraceOutboxSink(tmp_path)
    company_id = uuid.uuid4()
    tracers = [RunTracer(uuid.uuid4(), company_id, [sink]) for _ in range(4)]

    def emit_many(tracer):
        for i in range(50):
            tracer.emit("ROUTE", f"r{i}", "OK")

    threads = [threading.Thread(target=emit_many, args=(t,)) for t in tracers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for tracer in tracers:
        assert tracer.close(timeout=5)

    lines = (
        next((tmp_path / OUTBOX_DIR_NAME / str(company_id)).glob("*.jsonl"))
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(lines) == 200 and all(
        json.loads(line)["kind"] == "ROUTE" for line in lines
    )


# --- 통합: 실제 실행 상태 조회기(DB) ------------------------------------------------------


async def test_stale_run_from_db_is_traced_with_real_status_reader(
    db_session, tmp_path
):
    row = (
        await db_session.execute(
            text(
                "SELECT analysis_run_id, company_id FROM analysis_runs WHERE status = 'STALE' ORDER BY run_code LIMIT 1"
            )
        )
    ).first()
    if row is None:
        pytest.skip("STALE 실행이 없는 DB(overlay 미적재)라 건너뜀")
    reader = AnalysisRunStatusReader(
        async_sessionmaker(bind=db_session.bind), row.company_id
    )
    tracer = build_run_tracer(
        row.analysis_run_id, row.company_id, [TraceOutboxSink(tmp_path)], environ={}
    )

    result = await _run(
        _state(row.analysis_run_id, row.company_id), tracer, reader=reader
    )
    assert tracer.close()

    assert result["outcome"] == "STALE"
    assert [e.name for e in tracer.events if e.kind == "NODE_END"] == [
        "prepare",
        "coordinator",
    ]
    lines = next(
        (tmp_path / OUTBOX_DIR_NAME / str(row.company_id)).glob("*.jsonl")
    ).read_text(encoding="utf-8")
    last = json.loads(lines.splitlines()[-1])
    assert last["kind"] == "RUN_END" and last["summary"]["outcome"] == "STALE"
    assert last["analysis_run_id"] == str(row.analysis_run_id)
