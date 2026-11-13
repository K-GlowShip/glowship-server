import json
import uuid
from pathlib import Path

import pytest

from app.modules.delay_analysis.application.agent.fake_runtime import (
    FakeExplanation,
    FakeExplanationFailed,
    FakeHold,
    FakeScenario,
    fake_runtime,
)
from app.modules.delay_analysis.application.agent.graph import build_analysis_graph
from app.modules.delay_analysis.application.agent.retry import (
    PERMISSION_DENIED,
    REQUIRED_DATA_INVALID,
    TOOL_TEMPORARY_ERROR_EXHAUSTED,
    PermissionDeniedError,
    TransientToolError,
)
from app.modules.delay_analysis.application.agent.runtime import (
    RUNTIME_KEY,
    RuntimeWiringError,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.impact_schemas import (
    AtRiskOrders,
    IdentifyAtRiskOrdersInput,
)
from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    OpenOrder,
    OpenOrders,
    SalesHistory,
)
from app.modules.delay_analysis.application.agent.tools.registry import ToolSpec
from app.modules.delay_analysis.application.agent.tracing import RunTracer
from app.modules.delay_analysis.infrastructure.agent_trace_outbox import (
    TraceOutboxSink,
)

"""
    추적 기록(내부 outbox)에 고객 개인정보가 남지 않는지 (NFR-006, GLOW-57, docs/agent-flow/agent-tracing.md 2절)
      - NFR-006: 고객 이름·상세 주소·전화번호는 DB·로그에 포함하지 않는다. 추적 outbox 파일도 로그다.
      - 마스킹(TraceMasker)은 주문 코드·이메일만 가리고 이름·전화·주소 규칙은 없다(점검 F1). 지금은 업무 값이 추적에
        들어가는 경로가 구조적으로 막혀 있어(상태는 키 이름 + 안전한 키 값만, 오류 메시지는 버림) 새지 않는다.
        이 시험은 그 구조를 지킨다: 누가 상태 값·오류 문장·도구 출력·모델 출력 원문을 추적에 남기게 바꾸면 실패한다.
      - 실행이 업무 글을 추적기로 나를 수 있는 곳마다 가짜 개인정보를 넣고, 그래프를 실제로 돌려(build_analysis_graph)
        outbox 파일 전체에서 이름·전화·주소·이메일·원래 주문 코드가 0번 나오는지 본다.
          · 그래프 상태: 서비스 입력에 고객 정보처럼 생긴 키(customer_name·orders 등), 상태 키 data_warnings
            → RUN_START·NODE_END·RUN_END (TraceMasker.state)
          · 도구 오류 문장(일시 오류·권한 오류) → TOOL_ATTEMPT (tool_attempt 는 AttemptRecord 만 받음)
          · 도구 출력 값(T5 미출고 주문) → 상태 open_orders → NODE_END
          · 실패 요약 문구(보류 문구·설명 실패 기록) → 상태 failure
          · 설명 시도 기록의 프롬프트·모델 출력 원문, 검증 위치 → EXPLANATION_ATTEMPT (explanation_attempts)
          · 그래프 밖으로 나가는 예외 문장 → NODE_ERROR·RUN_ERROR (on_chain_error)
      - 이름·전화·주소는 마스킹 규칙이 없으므로 "버려야 하는 칸"에만 넣는다. 주문 코드·이메일은 값을 남기는 칸
        (설명 검증 위치)에도 넣어 가림까지 본다(ORD#·[EMAIL] 이 남았는지 양성 확인).
      - model_usage 는 summarize_usage 결과(숫자·정해진 코드·모델 이름)만 받으며, 그 정리는 tests/core/test_llm_usage.py 가 본다.
      - 빈 파일로 통과하지 않게 사건 수·종류와 넣은 키 이름이 추적까지 갔는지를 함께 단언한다.
"""

NAME = "홍길동테스트"
PHONE = "010-9876-5432"
ADDRESS = "서울특별시 가상구 시험로 77"
EMAIL = "pii-probe@example.com"
ORDER_CODE = "A-ORD-20261113-0777"  # 주문 코드 형식 X-ORD-YYYYMMDD-NNNN
PROBES = {
    "이름": NAME,
    "전화": PHONE,
    "주소": ADDRESS,
    "이메일": EMAIL,
    "주문 코드": ORDER_CODE,
}
CUSTOMER_TEXT = (
    f"주문 {ORDER_CODE} 고객 {NAME} 연락처 {PHONE} 주소 {ADDRESS} 메일 {EMAIL}"
)
# 서비스 입력에 섞어 넣는 고객 정보처럼 생긴 키(그래프 상태 정의 밖)
INJECTED_STATE_KEYS = (
    "customer_name",
    "customer_phone",
    "shipping_address",
    "customer_email",
    "order_code",
    "orders",
    "request_reason",
)
PROBE_WARNINGS = (
    {
        "code": "PROBE_WARNING",
        "count": 1,
        "detail": CUSTOMER_TEXT,
        "order_code": ORDER_CODE,
    },
)


class _Status:
    def __init__(self, status: str = "ANALYZING_IMPACT"):
        self.status = status

    async def get_status(self, analysis_run_id):
        return self.status


def _probe_state() -> dict:
    return {
        "analysis_run_id": uuid.uuid4(),
        "company_id": uuid.uuid4(),
        "delay_event_id": uuid.uuid4(),
        "operating_rule_set_id": uuid.uuid4(),
        "customer_name": NAME,
        "customer_phone": PHONE,
        "shipping_address": ADDRESS,
        "customer_email": EMAIL,
        "order_code": ORDER_CODE,
        "orders": [
            {
                "order_code": ORDER_CODE,
                "customer_name": NAME,
                "phone": PHONE,
                "address": ADDRESS,
                "email": EMAIL,
            }
        ],
        "request_reason": CUSTOMER_TEXT,
    }


def _probe_attempts(*, passed: bool) -> list[dict]:
    """엔진 설명 생성기 시도 기록과 같은 모양. 원문 칸(prompt_text·raw_output)과 검증 위치에 가짜 값을 넣는다."""
    failed = {
        "codes": [
            ["VALUE_MISMATCH", f"/risk/{ORDER_CODE}/promised_date"],
            ["FORBIDDEN_EXPRESSION", f"risk_summary#s1 {EMAIL}"],
        ],
        "model_provider": "mock",
        "model_id": "mock-template-v1",
        "prompt_template_id": "explain_ready_v1",
        "prompt_template_version": "1.0.0",
        "prompt_template_sha256": "abc",
        "prompt_text": f"규칙 1. 입력 문서만 쓴다. {CUSTOMER_TEXT}",
        "raw_output": f'{{"risk_summary": "{CUSTOMER_TEXT}"}}',
    }
    attempts = [{"retry_no": 1, **failed}]
    if passed:
        attempts.append({"retry_no": 2, **failed, "codes": []})
    return attempts


def _probe_tools(*, t7_error: Exception | None = None) -> dict[ToolName, ToolSpec]:
    """T4 는 첫 시도에 고객 정보가 든 일시 오류를 낸 뒤 성공, T5 출력 값에 고객 정보가 섞인 글을 넣는다.

    가짜 도구 출력은 형식 검사를 하지 않으므로(model_construct) 주문 코드 칸에 고객 정보가 섞인 글을 넣어,
    도구 출력 값이 추적으로 새는 경로가 생기면 잡는다. t7_error 를 주면 T7 이 그 오류로 멈춘다.
    """
    calls = {"t4": 0}

    async def t4(ctx, payload):
        calls["t4"] += 1
        if calls["t4"] == 1:
            raise TransientToolError(f"원천 응답 지연: {CUSTOMER_TEXT}")
        return SalesHistory.model_construct()

    async def t5(ctx, payload):
        return OpenOrders.model_construct(
            orders=[OpenOrder.model_construct(order_code=CUSTOMER_TEXT)],
            allocation_state=[],
        )

    tools = {
        ToolName.GET_SALES_HISTORY: ToolSpec(t4, None, SalesHistory),
        ToolName.GET_OPEN_ORDERS: ToolSpec(t5, None, OpenOrders),
    }
    if t7_error is not None:

        async def t7(ctx, payload):
            raise t7_error

        tools[ToolName.IDENTIFY_AT_RISK_ORDERS] = ToolSpec(
            t7, IdentifyAtRiskOrdersInput, AtRiskOrders
        )
    return tools


async def _traced_run(tmp_path: Path, runtime_of, *, reader=None) -> tuple:
    state = _probe_state()
    tracer = RunTracer(
        state["analysis_run_id"], state["company_id"], [TraceOutboxSink(tmp_path)]
    )
    config = {
        "configurable": {
            "run_status_reader": reader or _Status(),
            RUNTIME_KEY: runtime_of(tracer),
        },
        "callbacks": [tracer.callback_handler()],
    }
    try:
        result = await build_analysis_graph().ainvoke(state, config=config)
    finally:
        assert tracer.close(timeout=5)
    return result, tracer


def _outbox_events(tmp_path: Path, tracer: RunTracer) -> tuple[str, list[dict]]:
    """outbox 파일 전체(원문 + JSON 을 풀어 다시 쓴 글)와 사건 목록. 빈 파일이면 실패한다."""
    files = sorted(tmp_path.rglob("*.jsonl"))
    raw = "".join(path.read_text(encoding="utf-8") for path in files)
    events = [json.loads(line) for line in raw.splitlines()]
    assert files and len(events) == len(tracer.events) > 0
    assert [e["seq"] for e in events] == [e.seq for e in tracer.events]
    return raw + json.dumps(events, ensure_ascii=False), events


def _assert_no_personal_data(text: str, events: list[dict]) -> None:
    # 어느 사건의 어느 칸으로 샜는지 함께 보인다(실패하면 사람이 경로를 정해야 함)
    leaks = [
        (event["seq"], event["kind"], event["name"], field, label)
        for event in events
        for field, value in event.items()
        for label, probe in PROBES.items()
        if probe in json.dumps(value, ensure_ascii=False)
    ]
    assert leaks == []
    assert {
        label: text.count(probe) for label, probe in PROBES.items()
    } == dict.fromkeys(PROBES, 0)


def _of(events: list[dict], kind: str, name: str | None = None) -> list[dict]:
    return [e for e in events if e["kind"] == kind and name in (None, e["name"])]


# --- 정상 실행(READY_FOR_REVIEW) -------------------------------------------------------------


async def test_ready_run_trace_keeps_no_customer_personal_data(tmp_path):
    def runtime_of(tracer):
        runtime = fake_runtime(
            FakeScenario(data_warnings=PROBE_WARNINGS),
            tools=_probe_tools(),
            tracer=tracer,
        )

        async def explain(ctx, now):
            # make_explainer 처럼 시도 기록을 추적기에 넘긴다(첫 시도 검증 실패 → 둘째 통과)
            ctx.tracer.explanation_attempts(_probe_attempts(passed=True))
            return FakeExplanation(
                "fake/model",
                "fake@1",
                {"decision_signature_hash": ctx.engine.analysis["sha"]},
                now,
            )

        runtime.explain = explain
        return runtime

    result, tracer = await _traced_run(tmp_path, runtime_of)
    text, events = _outbox_events(tmp_path, tracer)

    _assert_no_personal_data(text, events)
    assert result["outcome"] == "READY_FOR_REVIEW"
    # 넣은 값이 실제로 추적기까지 갔음: 키 이름은 남고 값은 버려짐
    (run_start,) = _of(events, "RUN_START")
    assert set(INJECTED_STATE_KEYS) <= set(run_start["summary"]["keys"])
    (prepare_end,) = _of(events, "NODE_END", "prepare")
    assert "data_warnings" in prepare_end["summary"]["keys"]
    (impact_end,) = _of(events, "NODE_END", "inventory_impact")
    assert "open_orders" in impact_end["summary"]["keys"]
    assert [
        (e["attempt"], e["status"], e["error_code"])
        for e in _of(events, "TOOL_ATTEMPT", ToolName.GET_SALES_HISTORY.value)
    ] == [(1, "RETRY", TOOL_TEMPORARY_ERROR_EXHAUSTED), (2, "OK", None)]
    # 값을 남기는 칸(검증 위치)의 주문 코드·이메일은 가린 값으로 남음
    first, second = _of(events, "EXPLANATION_ATTEMPT")
    assert first["summary"]["codes"][0].startswith("VALUE_MISMATCH@/risk/ORD#")
    assert first["summary"]["codes"][1].endswith(" [EMAIL]")
    assert second["status"] == "OK"
    assert events[-1]["kind"] == "RUN_END"
    assert events[-1]["summary"]["outcome"] == "READY_FOR_REVIEW"


# --- 실패·보류로 끝나는 실행(실패 요약 문구) ---------------------------------------------------


class _HoldPreparer:
    async def judge_no_delay(self):
        return None

    async def build_engine_input(self):
        return FakeHold(
            REQUIRED_DATA_INVALID,
            f"차단 검증 오류: {CUSTOMER_TEXT}",
            data_warnings=PROBE_WARNINGS,
        )


def _hold_runtime(tracer):
    runtime = fake_runtime(tools=_probe_tools(), tracer=tracer)
    runtime.preparer = _HoldPreparer()
    return runtime


def _tool_failure_runtime(tracer):
    error = PermissionDeniedError(f"고객사 범위 밖: {CUSTOMER_TEXT}")
    return fake_runtime(tools=_probe_tools(t7_error=error), tracer=tracer)


def _explanation_failure_runtime(tracer):
    runtime = fake_runtime(tools=_probe_tools(), tracer=tracer)

    async def explain(ctx, now):
        ctx.tracer.explanation_attempts(_probe_attempts(passed=False))
        return FakeExplanationFailed("fake/model", "fake@1", f"1:FAKE@{CUSTOMER_TEXT}")

    runtime.explain = explain
    return runtime


@pytest.mark.parametrize(
    ("runtime_of", "outcome", "failure_carries_probe"),
    [
        (_hold_runtime, "ON_HOLD", True),  # 보류 문구(판정한 쪽 문구 그대로)
        (_tool_failure_runtime, "FAILED", False),  # 도구 권한 오류(문구는 고정)
        (_explanation_failure_runtime, "FAILED", True),  # 설명 3회 실패 기록
    ],
    ids=["hold", "tool_failure", "explanation_failure"],
)
async def test_stopped_run_trace_keeps_no_customer_personal_data(
    tmp_path, runtime_of, outcome, failure_carries_probe
):
    result, tracer = await _traced_run(tmp_path, runtime_of)
    text, events = _outbox_events(tmp_path, tracer)

    _assert_no_personal_data(text, events)
    assert result["outcome"] == outcome
    # 실패 요약에 실제로 가짜 값이 실렸음(이 문구가 추적에 가면 안 됨)
    assert (NAME in result["failure"].failure_message) is failure_carries_probe
    assert set(INJECTED_STATE_KEYS) <= set(
        _of(events, "RUN_START")[0]["summary"]["keys"]
    )
    assert "failure" in events[-1]["summary"]["keys"]
    assert (events[-1]["kind"], events[-1]["summary"]["outcome"]) == (
        "RUN_END",
        outcome,
    )
    if runtime_of is _tool_failure_runtime:
        assert [
            (e["status"], e["error_code"])
            for e in _of(events, "TOOL_ATTEMPT", ToolName.IDENTIFY_AT_RISK_ORDERS.value)
        ] == [("ERROR", PERMISSION_DENIED)]


# --- 그래프 밖으로 나가는 예외(오류 메시지) -----------------------------------------------------


async def test_node_error_trace_keeps_no_customer_personal_data(tmp_path):
    # 실행 도구 묶음 연결 실수(RuntimeWiringError)만 그래프 밖으로 나가 NODE_ERROR·RUN_ERROR 를 낸다(이슈 #57 1번)
    class Broken:
        async def get_status(self, analysis_run_id):
            raise RuntimeWiringError(f"상태 조회 연결 실수: {CUSTOMER_TEXT}")

    def runtime_of(tracer):
        return fake_runtime(tools=_probe_tools(), tracer=tracer)

    with pytest.raises(RuntimeWiringError):
        await _traced_run(tmp_path, runtime_of, reader=Broken())
    files = sorted(tmp_path.rglob("*.jsonl"))
    assert files  # 예외로 끝나도 쓴 사건은 파일에 있음
    raw = "".join(path.read_text(encoding="utf-8") for path in files)
    events = [json.loads(line) for line in raw.splitlines()]

    _assert_no_personal_data(raw + json.dumps(events, ensure_ascii=False), events)
    errors = [e for e in events if e["kind"] in ("NODE_ERROR", "RUN_ERROR")]
    assert {e["kind"] for e in errors} == {"NODE_ERROR", "RUN_ERROR"}
    assert all(e["error_code"] == "RuntimeWiringError" for e in errors)
