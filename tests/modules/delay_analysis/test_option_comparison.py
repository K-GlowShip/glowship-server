from collections import defaultdict
from datetime import date
from itertools import product

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent.tools.option_comparison import (
    compare_options,
)
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    OPTION_ORDER,
    CompareOptionsInput,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import OptionType
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    import_engine_module,
    read_manifest,
    resolve_engine_dir,
    verify_engine_dir,
)

"""
    T11 compare_options (docs/agent-flow/tool-contracts.md 7절)
      - 정렬 기준, KEEP_PLAN 위치, 순위 없는 대응안, 출력 순서, 정렬 값 없는 FEASIBLE 을 본다.
      - 엔진 대조는 GLOWSHIP_ENGINE_DIR 이 있을 때만, 저장값 대조는 DB 에 대응안이 있을 때만 돈다(읽기 전용).
"""

KEEP = "KEEP_PLAN"
PRE = "PRE_DEPARTURE_AIR"
KR = "KR_AIR_REPLENISH"
OTHER = "OTHER_REGION_FULFILL"
DAY_1 = date(2026, 1, 10)
DAY_2 = date(2026, 1, 11)


def _feasible(option_type, recovered=0, reduction=0, delivery=DAY_1) -> dict:
    return {
        "option_type": option_type,
        "feasibility_status": "FEASIBLE",
        "delay_induced_recovered_orders": recovered,
        "reduction_committed_total": reduction,
        "expected_delivery_date": delivery,
    }


def _not_ranked(option_type, status="NOT_FEASIBLE") -> dict:
    return {
        "option_type": option_type,
        "feasibility_status": status,
        "delay_induced_recovered_orders": None,
        "reduction_committed_total": None,
        "expected_delivery_date": None,
    }


def _ranks(*options: dict) -> dict:
    data = CompareOptionsInput.model_validate({"options": list(options)})
    return {
        o.option_type.value: o.recommendation_rank
        for o in compare_options(data).options
    }


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"recovered": 2}, {"recovered": 1, "reduction": 9}),
        ({"reduction": 2}, {"reduction": 1, "delivery": DAY_1}),
        ({"delivery": DAY_1}, {"delivery": DAY_2}),
    ],
)
def test_sort_values_decide_the_order(first, second):
    # 앞선 기준이 같을 때 다음 기준으로 정한다. 고정 순서로는 KR 이 OTHER 보다 앞이므로 OTHER 를 이기게 둔다.
    assert _ranks(
        _feasible(KEEP), _feasible(KR, **second), _feasible(OTHER, **first)
    ) == {
        KEEP: 3,
        KR: 2,
        OTHER: 1,
    }


def test_fixed_order_breaks_ties():
    assert _ranks(_feasible(KEEP), _feasible(PRE), _feasible(KR), _feasible(OTHER)) == {
        KEEP: 4,
        PRE: 1,
        KR: 2,
        OTHER: 3,
    }


def test_keep_plan_is_last_even_with_the_best_values():
    assert _ranks(_feasible(KEEP, 9, 9), _feasible(OTHER)) == {KEEP: 2, OTHER: 1}


def test_keep_plan_is_recommended_when_it_is_the_only_feasible_option():
    data = CompareOptionsInput.model_validate(
        {"options": [_feasible(KEEP), _not_ranked(OTHER)]}
    )
    keep, other = compare_options(data).options
    assert (keep.recommendation_rank, keep.is_recommended) == (1, True)
    assert (other.recommendation_rank, other.is_recommended) == (None, False)


def test_only_feasible_options_are_ranked():
    assert _ranks(
        _feasible(KEEP),
        _not_ranked(KR),
        _not_ranked(OTHER, "NEEDS_CHECK"),
    ) == {KEEP: 1, KR: None, OTHER: None}


def test_output_keeps_input_order_and_recommends_rank_one():
    data = CompareOptionsInput.model_validate(
        {"options": [_feasible(KEEP), _feasible(KR), _feasible(OTHER, 1)]}
    )
    options = compare_options(data).options
    assert [o.option_type.value for o in options] == [KEEP, KR, OTHER]
    assert [o.recommendation_rank for o in options] == [3, 2, 1]
    assert [o.is_recommended for o in options] == [False, False, True]


@pytest.mark.parametrize(
    "field",
    [
        "delay_induced_recovered_orders",
        "reduction_committed_total",
        "expected_delivery_date",
    ],
)
def test_feasible_option_without_a_sort_value_stops(field):
    option = _feasible(OTHER) | {field: None}
    with pytest.raises(ValueError, match="정렬 값"):
        _ranks(_feasible(KEEP), option)


def test_keep_plan_sort_values_are_not_needed():
    keep = _feasible(KEEP) | {"expected_delivery_date": None}
    assert _ranks(keep, _not_ranked(OTHER)) == {KEEP: 1, OTHER: None}


# --- 엔진 대조 (GLOWSHIP_ENGINE_DIR 이 있을 때만) ------------------------------------------------
def _states(option_type: str, optional: bool) -> list[dict | None]:
    feasible = [
        _feasible(option_type, recovered, reduction, delivery)
        for recovered, reduction, delivery in product((0, 1), (0, 1), (DAY_1, DAY_2))
    ]
    not_ranked = [
        _not_ranked(option_type),
        _not_ranked(option_type, "NEEDS_CHECK"),
    ]
    return feasible + not_ranked + ([None] if optional else [])


def _engine_option(option: dict) -> dict:
    return {
        "option_type": option["option_type"],
        "status": option["feasibility_status"],
        "delay_induced_recovered": option["delay_induced_recovered_orders"],
        "committed_total": option["reduction_committed_total"],
        "supply_date": option["expected_delivery_date"],
    }


def test_ranks_match_engine():
    configured = get_settings().glowship_engine_dir
    if configured is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 엔진 대조를 건너뜀")
    engine_dir = resolve_engine_dir(configured)
    verify_engine_dir(engine_dir, read_manifest())
    engine = import_engine_module(engine_dir, "options")

    for pre, kr, other in product(
        _states(PRE, True), _states(KR, True), _states(OTHER, False)
    ):
        options = [o for o in (_feasible(KEEP), pre, kr, other) if o]
        engine_options = [_engine_option(o) for o in options]
        engine._rank(engine_options)

        data = CompareOptionsInput.model_validate({"options": options})
        ours = compare_options(data).options

        assert [(o.recommendation_rank, o.is_recommended) for o in ours] == [
            (o["rank"], o["recommended"]) for o in engine_options
        ], options


# --- 저장값 대조 (읽기 전용) ---------------------------------------------------------------------
_STORED_OPTIONS = text(
    """
    SELECT analysis_run_id, option_type, feasibility_status,
           delay_induced_recovered_orders, reduction_committed_total,
           expected_delivery_date, recommendation_rank, is_recommended
    FROM response_options
    """
)


async def test_ranks_match_stored_options(db_session):
    rows = (await db_session.execute(_STORED_OPTIONS)).mappings().all()
    if not rows:
        pytest.skip("대응안이 없는 DB")
    runs = defaultdict(list)
    for row in rows:
        runs[row["analysis_run_id"]].append(row)

    for run_id, stored in runs.items():
        stored.sort(key=lambda row: OPTION_ORDER.index(OptionType(row["option_type"])))
        data = CompareOptionsInput.model_validate(
            {
                "options": [
                    {
                        "option_type": row["option_type"],
                        "feasibility_status": row["feasibility_status"],
                        "delay_induced_recovered_orders": row[
                            "delay_induced_recovered_orders"
                        ],
                        "reduction_committed_total": row["reduction_committed_total"],
                        "expected_delivery_date": row["expected_delivery_date"],
                    }
                    for row in stored
                ]
            }
        )

        assert [
            (o.recommendation_rank, o.is_recommended)
            for o in compare_options(data).options
        ] == [(row["recommendation_rank"], row["is_recommended"]) for row in stored], (
            run_id
        )
