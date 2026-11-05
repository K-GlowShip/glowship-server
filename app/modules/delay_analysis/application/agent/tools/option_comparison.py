from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    OPTION_ORDER,
    CompareOptionsInput,
    OptionComparisonInput,
    OptionRank,
    OptionRanking,
)
from app.modules.delay_analysis.infrastructure.analysis_enums import (
    FeasibilityStatus,
    OptionType,
)

"""
    T11 compare_options (docs/agent-flow/tool-contracts.md 7절, 엔진 options._rank)
      - 입력만으로 계산하는 순수 함수다. 출력은 순위순이 아니라 입력과 같은 고정 순서다.
      - 순위는 입력의 FEASIBLE 로만 매기므로 "FEASIBLE 만 순위"(ck15)·"추천은 FEASIBLE 만"(ck4)이 함께 지켜진다.
      - 정렬 값이 null 인 FEASIBLE 은 T10 출력 오류라 ValueError 로 멈춘다(9절 Q15).
"""


def _sort_key(option: OptionComparisonInput) -> tuple:
    """지연 유발 회복 내림차순 → 확정 감소 내림차순 → 공급일 오름차순 → 고정 순서 (B-5)"""
    values = (
        option.delay_induced_recovered_orders,
        option.reduction_committed_total,
        option.expected_delivery_date,
    )
    if None in values:
        raise ValueError(f"{option.option_type} 은 FEASIBLE 인데 정렬 값이 없습니다")
    recovered, reduction, delivery_date = values
    return -recovered, -reduction, delivery_date, OPTION_ORDER.index(option.option_type)


def compare_options(data: CompareOptionsInput) -> OptionRanking:
    feasible = [
        o for o in data.options if o.feasibility_status == FeasibilityStatus.FEASIBLE
    ]
    ranked = sorted(
        (o for o in feasible if o.option_type != OptionType.KEEP_PLAN), key=_sort_key
    )
    ranked += [o for o in feasible if o.option_type == OptionType.KEEP_PLAN]
    ranks = {o.option_type: rank for rank, o in enumerate(ranked, start=1)}
    return OptionRanking(
        options=[
            OptionRank(
                option_type=o.option_type,
                recommendation_rank=ranks.get(o.option_type),
                is_recommended=ranks.get(o.option_type) == 1,
            )
            for o in data.options
        ]
    )
