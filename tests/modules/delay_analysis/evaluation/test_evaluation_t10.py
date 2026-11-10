import copy

import pytest
from evaluation_adapters import engine_outcomes, outcomes_input

from app.modules.delay_analysis.application.agent.tools.option_outcomes import (
    calculate_option_outcomes,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

"""
    평가 시나리오의 대응안 결과(T10)를 엔진 options.evaluate 와 대조 (GLOW-133)
      - 시나리오 세계(snap)를 T1~T9 출력으로 옮겨 T10 을 부르고, 엔진 analysis["options"]를 storage.py 변환으로 옮긴 값과
        대응안 행·SKU 항목(sku_results.v1)·주문 결과 행까지 통째로 비교한다.
      - 대응안이 있는 시나리오 전부(GS-04 는 배정 시험, GS-06 은 보류라 없음). overlay 에 없는 경로를 여기서 본다:
        출항 전 항공 계산·회복(GS-02), 한국 항공 계산·회복(GS-03·GS-09·GS-10), 공여 전부 판정 불가(GS-07),
        여러 SKU 주문(GS-05), 공여 풀을 약속 순서로 나눠 씀(GS-11·탐침).
      - 변형: 시나리오 세계를 조금 바꿔 엔진을 다시 돌린다. 시나리오·overlay 어디에도 없는 경로(리드타임 없음, S ≥ E)를 본다.
"""

KEYS = ["GS-02", "GS-03", "GS-05", "GS-07", "GS-08", "GS-09A", "GS-09B", "GS-10", "GS-11", "GS-11-probe"]  # fmt: skip


async def _matches_engine(snap, analysis):
    assert analysis["options"]
    got = calculate_option_outcomes(await outcomes_input(snap, analysis))
    assert got.model_dump(mode="json") == engine_outcomes(analysis).model_dump(
        mode="json"
    )
    return {
        o.option_type.value: (o.feasibility_status.value, o.reason_code)
        for o in got.options
    }


@pytest.mark.parametrize("key", KEYS)
async def test_t10_matches_engine(scenario, key):
    snap, _replay, analysis = scenario(key)
    await _matches_engine(snap, analysis)


def _no_kr_and_donor_lead_times(snap):
    snap["lt"].update(oh_kr=None, oh_donor=None)


def _kr_release_on_e(snap):
    snap["planned_kr_release"] = snap["E"]


@pytest.mark.parametrize(
    ("key", "change", "expected"),
    [
        ("GS-03", _no_kr_and_donor_lead_times, {
            "KEEP_PLAN": ("FEASIBLE", None),
            "KR_AIR_REPLENISH": ("NEEDS_CHECK", "MISSING_OPERATING_RULE"),
            "OTHER_REGION_FULFILL": ("NEEDS_CHECK", "MISSING_OPERATING_RULE")}),
        ("GS-02", _kr_release_on_e, {
            "KEEP_PLAN": ("FEASIBLE", None),
            "PRE_DEPARTURE_AIR": ("NOT_FEASIBLE", "ARRIVES_AFTER_DELAYED_SHIPMENT"),
            "OTHER_REGION_FULFILL": ("NOT_FEASIBLE", "NO_TRANSFERABLE_STOCK")}),
    ],
)  # fmt: skip
async def test_t10_matches_engine_on_variants(scenario, key, change, expected):
    snap = copy.deepcopy(scenario(key)[0])
    change(snap)
    analysis = load_engine_module("pipeline").analyze(snap)
    assert await _matches_engine(snap, analysis) == expected
