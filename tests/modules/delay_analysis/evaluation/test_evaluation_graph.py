import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace

import evaluation_adapters as EA
import pytest
from real_path_support import fix_explanation_canary, require_engine
from test_evaluation_t9_t11 import ANSWERS

from app.modules.delay_analysis.application.agent.graph import build_analysis_graph
from app.modules.delay_analysis.application.agent.runtime import RUNTIME_KEY
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tools.registry import (
    ToolRegistry,
    ToolSpec,
)
from app.modules.delay_analysis.application.result_save.result_rows import (
    build_result_rows,
)
from app.modules.delay_analysis.application.run_start.engine_runtime import (
    build_engine_runtime,
)
from app.modules.delay_analysis.application.run_start.outcome_save import (
    analysis_result_from_state,
)
from app.modules.delay_analysis.infrastructure.engine.engine_input import (
    BaseDateResolution,
    EngineInput,
)

"""
    평가 시나리오 9건을 그래프 전체로 (GLOW-58 2단계 C 갈래)
      - 1단계는 도구(T9·T10·T11)와 설명을 하나씩 불러 정답표와 견줬다. 여기서는 같은 시나리오를 실제 묶음으로
        분석 준비 → 조정 → 재고 영향 → 대응안 → 비교 → 설명 → 검증까지 그래프 순서대로 돌린다.
      - 평가 시나리오는 DB 데이터가 아니다(엔진 fixture 가 스냅샷을 만든다). 그래서 DB 를 읽는 두 곳만 바꿔 끼운다:
        분석 준비는 fixture 의 스냅샷·엔진 판정을 엔진 입력으로 돌려주고, 조회 도구 T1~T5·T8 은 스냅샷에서 옮기는
        어댑터(evaluation_adapters, #90)를 쓴다. T6·T7·T9·T10·T11·T12·설명·판정 서명 대조는 실제 등록 그대로다.
        실제 조회 도구는 정답 실행 실제 경로 시험(test_evaluation_real_path)이 본다.
      - 견주는 것: 재고 영향 완료 상태, T10 출력 = 엔진 대응안, T11 순위·추천 = 정답표, 판정 서명 = 엔진,
        종료 상태, 그래프 상태 → 저장 행 변환.
      - 9건 모두 설명·T12 까지 지나 READY_FOR_REVIEW 다. 엔진 설명 검증기 수정판(#114) 전에는 GS-05 만 통과했고
        나머지 8건은 구분자 없는 SKU 코드(CREAM20 등) 안의 숫자를 수치로 세어 설명에서 3회 실패했다(1단계 문서 4.1절).
        그 8건은 strict xfail 로 두었다가 수정판이 들어온 뒤 READY 기대로 합쳤다. 설명 실패 갈래(FAILED·계산 결과 남음)는
        정답 실행 RT-2(test_evaluation_real_path)와 T13·T20 통합 시험이 본다.
"""

KEYS = [
    "GS-02",
    "GS-03",
    "GS-05",
    "GS-07",
    "GS-08",
    "GS-09A",
    "GS-09B",
    "GS-10",
    "GS-11",
]

RUN_ID = uuid.UUID(int=0x58)
COMPANY_ID = uuid.UUID(int=1)
SHIPMENT_ID = uuid.uuid5(RUN_ID, "ship")
NOW = datetime(2026, 3, 10, 0, 5, tzinfo=UTC)
T = ToolName

_states: dict[str, dict] = {}  # 키 → 그래프 마지막 상태(시나리오마다 한 번만 돌린다)


@pytest.fixture(autouse=True)
def _fixed_canary(glow50, monkeypatch):
    """glow50 이 엔진 경로가 없으면 건너뛴다. 설명 모델이 mock 이 아니어도 건너뛴다. 설명 canary 는 고정한다."""
    require_engine()
    fix_explanation_canary(monkeypatch)


# 아래 _engine_input·_ScenarioPreparer·_runtime·_state 와 KEYS·RUN_ID·COMPANY_ID·SHIPMENT_ID 는
# test_evaluation_external_instruction.py 도 가져다 쓴다(같은 구성을 두 벌 두지 않음). 이름·인자를 바꾸면 그 파일도 함께 고친다.
def _engine_input(snap: dict, analysis: dict) -> EngineInput:
    day = snap["D"] if isinstance(snap["D"], date) else date.fromisoformat(snap["D"])
    return EngineInput(
        analysis_run_id=RUN_ID,
        company_id=COMPANY_ID,
        delay_event_id=uuid.UUID(int=7),
        shipment_id=SHIPMENT_ID,
        base_date=day,
        base_date_resolution=BaseDateResolution(
            base_date=day,
            run_date=day,
            inventory_limit=None,
            order_limit=None,
            late_sources=(),
        ),
        input_seq=1,
        confirm_t=f"{day.isoformat()}T00:00:00Z",
        snap=snap,
        analysis=analysis,
        store=SimpleNamespace(
            ships={str(SHIPMENT_ID): {"shipment_code": "SHIP-DELAYED"}}
        ),
    )


class _ScenarioPreparer:
    """분석 준비: fixture 의 스냅샷·엔진 판정을 엔진 입력으로 돌려준다(DB 를 읽지 않음)."""

    def __init__(self, engine: EngineInput):
        self.engine = engine

    async def judge_no_delay(self):
        return None

    async def build_engine_input(self):
        return self.engine


def _runtime(snap: dict, analysis: dict):
    """실제 묶음에서 분석 준비와 조회 도구 T1~T5·T8 만 시나리오 스냅샷으로 바꾼다."""
    codes = sorted(snap["sku_ids"])
    run = SimpleNamespace(input_seq=1)
    runtime = build_engine_runtime(None, run, clock=lambda: NOW)
    runtime.preparer = _ScenarioPreparer(_engine_input(snap, analysis))

    def adapter(make):
        async def fn(ctx, payload):
            return make()

        return fn

    tools = dict(runtime.registry)
    for name, make in {
        T.GET_DELAY_CONTEXT: lambda: EA._delay_context(snap),
        T.GET_INVENTORY_SNAPSHOT: lambda: EA._inventory(snap, codes),
        T.GET_INBOUND_SCHEDULE: lambda: EA._inbound(snap, codes),
        T.GET_SALES_HISTORY: lambda: EA._sales(snap, codes),
        T.GET_OPEN_ORDERS: lambda: EA._open_orders(snap),
        T.GET_OPERATING_RULES: lambda: EA._rules(snap, codes),
    }.items():
        spec = tools[name]
        tools[name] = ToolSpec(adapter(make), spec.input_model, spec.output_model)
    runtime.registry = ToolRegistry(tools)
    return runtime


async def _state(scenario, key: str) -> dict:
    if key not in _states:
        snap, _replay, analysis = scenario(key)
        _states[key] = await build_analysis_graph().ainvoke(
            {
                "analysis_run_id": RUN_ID,
                "company_id": COMPANY_ID,
                "delay_event_id": uuid.UUID(int=7),
                "operating_rule_set_id": uuid.UUID(int=8),
                "step": AnalysisStep.VALIDATING,
            },
            {"configurable": {RUNTIME_KEY: _runtime(snap, analysis)}},
        )
    return _states[key]


@pytest.mark.parametrize("key", KEYS)
async def test_graph_computes_scenario_like_engine_and_answer_sheet(scenario, key):
    """C1: 그래프가 낸 재고 영향·대응안·순위·판정 서명이 엔진·정답표와 같고, 비교 노드를 지나 설명까지 간다."""
    _snap, _replay, analysis = scenario(key)
    state = await _state(scenario, key)

    assert state["inventory_impact"].completed_status == "READY_FOR_REVIEW"
    assert state["option_outcomes"].model_dump(mode="json") == EA.engine_outcomes(
        analysis
    ).model_dump(mode="json")
    assert EA.ranking_rows(state["option_ranking"]) == ANSWERS[key]["t11"]
    decision = state["decision"]
    assert decision.decision_signature_sha256 == analysis["sha"]
    assert decision.decision_signature == analysis["signature"]
    assert "explain" in state["visited_nodes"]  # 비교 노드(엔진 판정 서명 대조)를 통과


@pytest.mark.parametrize("key", KEYS)
async def test_graph_reaches_ready_with_real_explanation(scenario, key):
    """C2: 실제 설명·T12 까지 지나 READY_FOR_REVIEW."""
    state = await _state(scenario, key)

    assert state["outcome"] == AnalysisOutcome.READY_FOR_REVIEW
    assert state["visited_nodes"][-1] == "validate"
    explanation = state["explanation"].final_explanation_json
    assert explanation["schema_version"] == "explanation.v1"
    assert explanation["analysis_run_id"] == str(RUN_ID)
    assert explanation["decision_signature_hash"] == scenario(key)[2]["sha"]


@pytest.mark.parametrize("key", KEYS)
async def test_graph_state_converts_to_result_rows(scenario, key):
    """C3: 그래프 상태 → 저장 입력 → 저장 행. 시나리오 ID 가 DB 에 없어 저장은 하지 않는다."""
    _snap, _replay, analysis = scenario(key)
    state = await _state(scenario, key)
    assert state["outcome"] == AnalysisOutcome.READY_FOR_REVIEW

    result = analysis_result_from_state(state)
    rows = build_result_rows(result, company_id=COMPANY_ID, analysis_run_id=RUN_ID)

    assert result.explanation is not None
    assert rows.run_values["decision_signature_sha256"] == analysis["sha"]
    assert len(rows.response_options) == len(analysis["options"])
    risks = state[
        "at_risk_orders"
    ].risks  # 저장 행은 주문 상세 단위라 위험 주문 수 이상
    assert risks and len(rows.at_risk_orders) >= len(risks)
    assert len(rows.inventory_impact_results) == len(state["inventory_impact"].items)
    recommended = [o for o in rows.response_options if o["is_recommended"]]
    expected = [k for k, v in ANSWERS[key]["t11"].items() if v["recommended"]]
    assert [o["option_type"] for o in recommended] == expected
