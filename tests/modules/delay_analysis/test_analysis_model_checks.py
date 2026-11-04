import re

import pytest
from sqlalchemy import CheckConstraint

import app.core.model_registry  # noqa: F401  (모든 모델 등록)
from app.core.orm import Base
from app.modules.delay_analysis.infrastructure import analysis_enums as E

"""
    분석 표 모델의 CHECK 허용 값 = 코드의 열거형 (0005, v6.2 DDL)
      - 모델 CHECK 식은 0005 마이그레이션(= v6.2 DDL)과 같은 문장입니다. 열거형이 DB 가 받는 값과 어긋나면
        저장할 때 CHECK 위반으로 실패하므로, 값 목록이 같은지 DB 없이 확인합니다.
      - v5 의 옛 값(EAST_FULFILL·FULL·PARTIAL·EAST_WAREHOUSE)이 다시 들어오면 여기서 걸립니다.
"""

CASES = [
    ("analysis_runs", "analysis_runs_ck1", E.TriggerType),
    ("analysis_runs", "analysis_runs_ck5", E.ExplanationValidationStatus),
    ("analysis_runs", "analysis_runs_ck9", E.AnalysisRunStatus),
    ("analysis_runs", "analysis_runs_ck12", E.FailedStep),
    ("analysis_runs", "analysis_runs_ck15", E.TriggerReason),
    ("analysis_runs", "analysis_runs_ck19", E.CompletedStatus),
    ("analysis_runs", "analysis_runs_ck20", E.ResultClass),
    ("analysis_runs", "analysis_runs_ck28", E.StaleReason),
    ("analysis_runs", "analysis_runs_ck30", E.StaleReasonCode),
    ("inventory_impact_results", "inventory_impact_results_ck1", E.CalculationStatus),
    ("inventory_impact_results", "inventory_impact_results_ck3", E.SkuScope),
    ("inventory_impact_results", "inventory_impact_results_ck5", E.SalesBasisType),
    ("at_risk_orders", "at_risk_orders_ck3", E.RiskAttribution),
    ("at_risk_orders", "at_risk_orders_ck4", E.WaitCause),
    ("response_options", "response_options_ck1", E.OptionType),
    ("response_options", "response_options_ck2", E.FeasibilityStatus),
    ("option_order_outcomes", "option_order_outcomes_ck1", E.OrderOutcome),
    ("option_order_outcomes", "option_order_outcomes_ck7", E.SupplySource),
    ("option_order_outcomes", "option_order_outcomes_ck8", E.LineSecurementStatus),
    ("option_order_outcomes", "option_order_outcomes_ck12", E.NotRecoveredReason),
]


def _check(table: str, name: str) -> str:
    for constraint in Base.metadata.tables[table].constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return str(constraint.sqltext)
    raise AssertionError(f"{table} 모델에 {name} 이 없습니다")


def _values(sql: str) -> set[str]:
    return set(re.findall(r"'([A-Z0-9_]+)'", sql))


@pytest.mark.parametrize(("table", "name", "enum"), CASES, ids=[c[1] for c in CASES])
def test_check_values_match_enum(table, name, enum):
    assert _values(_check(table, name)) == {member.value for member in enum}


def test_option_reason_codes_match_enum():
    """대응안 사유 코드는 NOT_FEASIBLE(ck6)·NEEDS_CHECK(ck7) 두 목록을 합친 것"""
    sql = _check("response_options", "response_options_ck6") + _check(
        "response_options", "response_options_ck7"
    )
    codes = _values(sql) - {"NOT_FEASIBLE", "NEEDS_CHECK"}
    assert codes == {member.value for member in E.OptionReasonCode}


@pytest.mark.parametrize(
    ("table", "removed"),
    [
        ("response_options", "partially_recovered_orders"),
        ("option_order_outcomes", "allocated_quantity"),
    ],
)
def test_v5_column_names_are_gone(table, removed):
    """v5 이름(0005 에서 바꿈)은 모델에 없어야 합니다"""
    assert removed not in Base.metadata.tables[table].columns
