import copy
import re
import types
import typing
from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import CheckConstraint, text

import app.core.model_registry  # noqa: F401  (모든 모델 등록)
from app.core.orm import Base
from app.modules.delay_analysis.application.agent.tools import (
    impact_schemas as I,
)
from app.modules.delay_analysis.application.agent.tools import (
    lookup_schemas as L,
)
from app.modules.delay_analysis.application.agent.tools import (
    option_schemas as OS,
)
from app.modules.delay_analysis.application.agent.tools.common import (
    TOOL_NUMBERS,
    RegionCode,
    ShipmentStage,
    ToolModel,
    TransportMode,
)
from app.modules.delay_analysis.infrastructure import analysis_enums as E
from app.modules.delay_analysis.infrastructure.stored_json_schemas import SkuResults
from app.modules.delay_analysis.presentation import analysis_run_schemas
from app.modules.order.infrastructure.order_enums import OrderLineStatus, SalesChannel

"""
    에이전트 도구 입출력 모델 = 계약 문서 (docs/agent-flow/tool-contracts.md, GLOW-112)
      1. 문서 표의 필드 이름과 모델 필드가 양방향으로 같은지 (문서만 고치거나 모델만 고치면 실패)
      2. 문서 5절·6.1절의 사유 집합과 모델 규칙이 같은지, 모델의 코드값이 DDL CHECK 값인지
      3. 값 모양(날짜·식별자·시각·raw)과 DDL CHECK 조합 규칙을 모델이 지키는지 (양성·음성)
      4. 실제 DB 의 overlay 결과 행이 T10 모델 검증을 모두 통과하는지 (DB 없으면 건너뜀)
"""

DOC = Path(__file__).resolve().parents[3] / "docs" / "agent-flow" / "tool-contracts.md"
O, F, R = E.OptionType, E.FeasibilityStatus, E.OptionReasonCode


# --- 문서 읽기 -----------------------------------------------------------------------------------


def _doc() -> str:
    return DOC.read_text(encoding="utf-8").replace("\r\n", "\n")


def _section(heading_prefix: str, doc: str | None = None) -> str:
    """'### 3.1' 처럼 시작하는 제목부터 다음 제목 전까지"""
    lines = (doc or _doc()).split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(heading_prefix))
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("#")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _tables(section: str) -> list[list[list[str]]]:
    """표마다 본문 행의 칸 목록 (머리 행·구분 행은 뺌)"""
    tables, current = [], []
    for line in section.split("\n") + [""]:
        if line.startswith("|"):
            current.append(
                [cell.strip() for cell in line.strip().strip("|").split("|")]
            )
        elif current:
            tables.append(current[2:])
            current = []
    return tables


def _ticks(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def _closure(paths: set[str]) -> set[str]:
    """'a[].b.c' 가 있으면 'a[]'·'a[].b' 도 있는 것으로 본다"""
    out = set()
    for path in paths:
        parts = path.split(".")
        out.update(".".join(parts[: i + 1]) for i in range(len(parts)))
    return out


def _doc_paths(rows: list[list[str]]) -> set[str]:
    """
    표 첫 칸의 필드 경로를 모은다.
      - `a[].b`·`c` 의 두 번째 이름은 첫 이름과 같은 부모(a[].)를 쓴다.
      - `…items[].x` 는 앞에서 나온 '…items[]' 로 끝나는 경로를 부모로 쓴다.
      - 형식 칸의 배열(`키`) 은 그 배열의 필드, `{a, b}` 는 그 배열 항목의 필드다.
    """
    seen: list[str] = []
    for row in rows:
        tokens = _ticks(row[0])
        if not tokens:
            continue
        first = tokens[0]
        if first.startswith("…"):
            rest = first[1:]
            head = rest.split(".")[0]
            base = next(p for p in reversed(seen) if p.endswith(head))
            first = base[: len(base) - len(head)] + rest
        parent = first[: first.rfind(".") + 1] if "." in first else ""
        paths = [first] + [parent + t for t in tokens[1:]]
        formats = row[1].split(" · ") if len(row) > 1 else [""]
        for i, path in enumerate(paths):
            seen.append(path)
            fmt = (
                formats[i]
                if len(formats) == len(paths)
                else row[1]
                if len(row) > 1
                else ""
            )
            if path.endswith("[]"):
                for keys in re.findall(r"배열\(([^)]*)\)", fmt):
                    seen.extend(f"{path}.{k}" for k in _ticks(keys))
                for names in re.findall(r"`\{([^}]*)\}`", fmt):
                    seen.extend(f"{path}.{n.strip()}" for n in names.split(","))
    return _closure(set(seen))


def _unwrap(annotation):
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
        elif origin in (typing.Union, types.UnionType):
            args = [a for a in typing.get_args(annotation) if a is not type(None)]
            annotation = args[0]
        else:
            return annotation


def _model_paths(model: type[BaseModel], prefix: str = "") -> set[str]:
    """모델 필드 경로. 목록은 '[]', 하위 모델은 '.' 로 잇는다. sku_results.v1 은 JSON 계약 v3 가 정하므로 들어가지 않는다."""
    out = set()
    for name, field in model.model_fields.items():
        annotation = _unwrap(field.annotation)
        path = prefix + name
        if typing.get_origin(annotation) is list:
            path += "[]"
            annotation = _unwrap(typing.get_args(annotation)[0])
        out.add(path)
        if (
            isinstance(annotation, type)
            and issubclass(annotation, BaseModel)
            and annotation is not SkuResults
        ):
            out |= _model_paths(annotation, path + ".")
    return out


# --- 1. 문서 표 = 모델 필드 ----------------------------------------------------------------------

WAREHOUSE_SUMMARY = {"warehouse_id", "warehouse_code", "region_code"}
FORECAST_PATH = {
    "stockout_date",
    "stockout_days",
    "safety_stock_breach_date",
    "min_gap",
    "daily_deficits[]",
    "daily_deficits[].date",
    "daily_deficits[].deficit",
}

OUTPUT_TABLES = [
    ("### 3.1", L.DelayContext),
    ("### 3.2", L.InventorySnapshot),
    ("### 3.3", L.InboundSchedule),
    ("### 3.4", L.SalesHistory),
    ("### 3.5", L.OpenOrders),
    ("### 3.6", L.OperatingRules),
    ("### 4.1", I.AtRiskOrders),
    ("### 4.2", I.InventoryImpact),
]


def _extra_paths(heading: str, doc: str) -> set[str]:
    """표 밖 문장으로 정한 하위 필드 (문장이 문서에 그대로 있을 때만 더한다)"""
    if heading == "### 3.1":
        assert "창고 요약 = `warehouse_id`, `warehouse_code`, `region_code`" in doc
        return {
            f"{w}.{f}"
            for w in ("destination_warehouse", "donor_warehouse", "kr_origin_warehouse")
            for f in WAREHOUSE_SUMMARY
        }
    if heading == "### 4.2":
        assert (
            "`stockout_date`, `stockout_days`, `safety_stock_breach_date`, `min_gap`"
            in doc
        )
        assert "`daily_deficits[]`(`{date, deficit}`" in doc
        return {
            f"items[].{side}.{f}"
            for side in ("baseline", "total")
            for f in FORECAST_PATH
        }
    return set()


@pytest.mark.parametrize(
    ("heading", "model"), OUTPUT_TABLES, ids=[h for h, _ in OUTPUT_TABLES]
)
def test_output_table_fields_match_model(heading, model):
    doc = _doc()
    documented = _doc_paths(_tables(_section(heading, doc))[0]) | _extra_paths(
        heading, doc
    )
    implemented = _model_paths(model)
    assert implemented - documented == set(), "모델에만 있는 필드"
    assert documented - implemented == set(), "문서에만 있는 필드"


def test_option_tool_tables_match_models():
    doc = _doc()
    t9 = _tables(_section("## 5.", doc))
    t10 = _tables(_section("## 6.", doc))
    t11 = _tables(_section("## 7.", doc))
    # T9 출력(두 번째 표) = 대응안 항목
    assert _doc_paths(t9[1]) == _model_paths(OS.OptionPrecheck)
    # T10 출력: 대응안 표 + 주문 결과 표(outcomes[] 항목)
    outcome_rows = {f"outcomes[].{p}" for p in _doc_paths(t10[1])}
    assert _doc_paths(t10[0]) | outcome_rows == _model_paths(OS.OptionOutcome)
    # T11 출력
    assert _doc_paths(t11[0]) == _model_paths(OS.OptionRank)


def _top_level(tokens: list[str]) -> set[str]:
    return {t.split("[")[0].split(".")[0] for t in tokens}


def test_input_fields_match_model():
    doc = _doc()
    t1_line = next(
        line
        for line in _section("### 3.1", doc).split("\n")
        if line.startswith("입력:")
    )
    assert _top_level(_ticks(t1_line)) == set(L.GetDelayContextInput.model_fields)

    t9_rows = _tables(_section("## 5.", doc))[0]
    t9_tokens = [t for row in t9_rows for t in _ticks(row[0])]
    assert _top_level(t9_tokens) == set(OS.CheckOptionFeasibilityInput.model_fields)

    t11_line = next(
        line for line in _section("## 7.", doc).split("\n") if line.startswith("입력:")
    )
    t11_tokens = [t for t in _ticks(t11_line) if t != "options[]"]
    assert set(t11_tokens) == set(OS.OptionComparisonInput.model_fields)


# 묶음 입력의 키 → 그 값을 내는 도구 (계약 9.1절)
INPUT_KEY_TOOL = {
    "delay_context": "T1",
    "inventory_snapshot": "T2",
    "inbound_schedule": "T3",
    "sales_history": "T4",
    "open_orders": "T5",
    "operating_rules": "T8",
    "inventory_impact": "T6",
    "at_risk_orders": "T7",
    "delay_induced_sku_codes": "T7",
    "option_feasibility": "T9",
}


@pytest.mark.parametrize(
    ("heading", "model"),
    [
        ("### 4.1", I.IdentifyAtRiskOrdersInput),
        ("### 4.2", I.CalculateInventoryImpactInput),
        ("## 6.", OS.CalculateOptionOutcomesInput),
    ],
)
def test_composite_input_uses_documented_tools(heading, model):
    line = next(
        line for line in _section(heading).split("\n") if line.startswith("입력:")
    )
    sentence = line.split(". ")[0]
    documented = set(re.findall(r"T\d+", sentence))
    assert {INPUT_KEY_TOOL[name] for name in model.model_fields} == documented


def test_tool_names_match_contract_table():
    rows = _tables(_section("## 2.", _doc()))[0]
    documented = {row[0]: _ticks(row[1])[0] for row in rows}
    assert documented == {number: name.value for name, number in TOOL_NUMBERS.items()}


def test_doc_field_check_catches_missing_and_extra_fields():
    """음성: 모델 필드를 하나 빼거나 문서에 가짜 필드를 넣으면 어긋남이 보인다"""
    rows = copy.deepcopy(_tables(_section("### 3.3"))[0])
    implemented = _model_paths(L.InboundSchedule)
    assert _doc_paths(rows) == implemented
    rows.append(["`inbound[].fake_field`", "text", "아니오", "", "", ""])
    assert _doc_paths(rows) - implemented == {"inbound[].fake_field"}

    class Missing(ToolModel):
        inbound: list[L.InboundShipment]

    assert _doc_paths(_tables(_section("### 3.3"))[0]) - _model_paths(Missing) == {
        "donor_next_inbound[]",
        "donor_next_inbound[].sku_code",
        "donor_next_inbound[].next_inbound_date",
    }


# --- 2. 사유 집합·코드값 -------------------------------------------------------------------------

REASON_CODES = {c.value for c in E.OptionReasonCode}


def test_table_a_matches_option_reasons():
    rows = _tables(_section("### 6.1"))[0]
    documented = {}
    for row in rows:
        option = O(_ticks(row[0])[0])
        documented[option] = {
            F.NEEDS_CHECK: {R(t) for t in _ticks(row[1]) if t in REASON_CODES},
            F.NOT_FEASIBLE: {R(t) for t in _ticks(row[2]) if t in REASON_CODES},
        }
    implemented = {
        option: {
            status: OS.OPTION_REASONS[option].get(status, set())
            for status in (F.NEEDS_CHECK, F.NOT_FEASIBLE)
        }
        for option in O
    }
    assert documented == implemented
    assert all(OS.OPTION_REASONS[o][F.FEASIBLE] == {None} for o in O)


def test_table_b_matches_not_recovered_reasons():
    section = _section("### 6.1")
    rows = _tables(section)[1]
    documented: dict[E.OptionType, set[E.NotRecoveredReason]] = {}
    options: list[E.OptionType] = []
    for row in rows:
        if _ticks(row[0]):
            options = [O(t) for t in _ticks(row[0])]
        condition = row[2]
        only = re.match(r"\(`([A-Z_]+)`만\)", condition)
        targets = [O(only.group(1))] if only else options
        after = condition.split("→")[-1]
        reason = E.NotRecoveredReason(_ticks(after)[0])
        for option in targets:
            documented.setdefault(option, set()).add(reason)
    assert documented == OS.NOT_RECOVERED_REASONS


def test_precheck_table_matches_model():
    rows = _tables(_section("## 5."))[2]
    documented: dict = {}
    option = None
    for row in rows:
        if _ticks(row[0]):
            option = O(_ticks(row[0])[0])
        tokens = _ticks(row[2])
        precheck = OS.Precheck(tokens[0])
        reason = R(tokens[1]) if len(tokens) > 1 and tokens[1] in REASON_CODES else None
        documented.setdefault(option, {}).setdefault(precheck, set()).add(reason)
    assert documented == OS.PRECHECK_REASONS


def test_precheck_values_match_doc():
    rows = _tables(_section("## 5."))[1]
    precheck_row = next(row for row in rows if _ticks(row[0]) == ["precheck"])
    # 값 바로 뒤에 괄호 설명이 붙은 것만 사전 판정 값이다 (설명 안의 `NOT_FEASIBLE` 은 뺌)
    assert set(re.findall(r"`([A-Z_]+)`\(", precheck_row[3])) == {
        p.value for p in OS.Precheck
    }


def _check_values(table: str, name: str) -> set[str]:
    for constraint in Base.metadata.tables[table].constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == name:
            return set(re.findall(r"'([A-Z0-9_]+)'", str(constraint.sqltext)))
    raise AssertionError(f"{table} 모델에 {name} 이 없습니다")


@pytest.mark.parametrize(
    ("values", "table", "name"),
    [
        (set(typing.get_args(TransportMode)), "shipments", "shipments_ck1"),
        (set(typing.get_args(ShipmentStage)), "shipments", "shipments_ck2"),
        (
            set(typing.get_args(ShipmentStage)),
            "shipment_schedule_snapshots",
            "shipment_schedule_snapshots_ck1",
        ),
        (set(typing.get_args(RegionCode)), "warehouses", "warehouses_ck2"),
        ({c.value for c in SalesChannel}, "orders", "orders_ck2"),
        ({c.value for c in OrderLineStatus}, "order_lines", "order_lines_ck3"),
    ],
    ids=[
        "transport_mode",
        "shipment_stage",
        "snapshot_stage",
        "region_code",
        "sales_channel",
        "line_status",
    ],
)
def test_literal_values_match_ddl(values, table, name):
    assert values == _check_values(table, name)


def test_reason_sets_are_within_ddl():
    """
    표 A·B 와 사전 판정 사유가 DDL 이 받는 값 안에 있는지 직접 본다.
    문서 표와 모델을 함께 잘못 고치면(예: Q9 조합 허용) 문서 = 모델 시험은 통과하므로 DDL 과 따로 대조한다.
    """
    status_words = {s.value for s in F}
    not_feasible = (
        _check_values("response_options", "response_options_ck6") - status_words
    )
    needs_check = (
        _check_values("response_options", "response_options_ck7") - status_words
    )
    not_recovered = _check_values("option_order_outcomes", "option_order_outcomes_ck12")
    for option, by_status in OS.OPTION_REASONS.items():
        assert by_status.get(F.NOT_FEASIBLE, set()) <= not_feasible, option
        assert by_status.get(F.NEEDS_CHECK, set()) <= needs_check, option
    for option, reasons in OS.NOT_RECOVERED_REASONS.items():
        assert {r.value for r in reasons} <= not_recovered, option
    # 사전 판정 NO_SUPPLY 사유는 T10 에서 NOT_FEASIBLE 사유가 되고, NEEDS_CHECK 사유는 그대로 NEEDS_CHECK 사유다(5·6절)
    for option, by_precheck in OS.PRECHECK_REASONS.items():
        assert by_precheck.get(OS.Precheck.NO_SUPPLY, set()) <= not_feasible, option
        assert by_precheck.get(OS.Precheck.NEEDS_CHECK, set()) <= needs_check, option


# 문서에 나오는 대문자 코드값 가운데 서버 열거형이 아닌 것 (어디 값인지 밝혀 둔다)
NON_ENUM_CODES = {
    "KR",  # region_code (Literal)
    "WEST",
    "EAST",
    "OCEAN",  # transport_mode (Literal)
    "AIR",
    "PRE_DEPARTURE",  # stage (Literal)
    "AT_SEA",
    "AT_PORT",
    "IN_AIR",
    "RECEIVED",
    "AMAZON",  # sales_channel (order_enums)
    "SEPHORA",
    "ACTIVE",  # line_status (order_enums)
    "REMOVED_AT_SOURCE",
    "US_3PL",  # warehouses.warehouse_type
    "KR_ORIGIN",
    "OUTBOUND_HANDLING",  # operating_lead_times.lead_time_type
    "CUSTOMER_DELIVERY",
    "AIR_TRANSIT",
    "INBOUND_HANDLING",
    "BUSINESS_DAY",  # operating_lead_times.day_basis
    "CALENDAR_DAY",
    "REQUIRED_DATA_INVALID",  # 입력 검증 보류 사유(엔진 Hold, GLOW-100)
}


def _doc_codes(doc: str) -> set[str]:
    return {
        t
        for t in re.findall(r"`([^`\n]+)`", doc)
        if re.fullmatch(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*", t) and len(t) >= 2
    }


def _known_codes() -> set[str]:
    enums = [
        getattr(E, n)
        for n in dir(E)
        if isinstance(getattr(E, n), type) and issubclass(getattr(E, n), E.StrEnum)
    ]
    return (
        {m.value for e in enums for m in e}
        | {p.value for p in OS.Precheck}
        | NON_ENUM_CODES
    )


def test_doc_codes_are_known():
    assert _doc_codes(_doc()) - _known_codes() == set()


def test_doc_code_check_catches_unknown_code():
    """음성: 문서에 없는 코드값을 넣으면 잡힌다"""
    assert _doc_codes(_doc() + "\n| x | `FAKE_TOOL_CODE` |") - _known_codes() == {
        "FAKE_TOOL_CODE"
    }


def test_sku_results_model_is_shared():
    """응답 스키마와 도구 모델이 같은 sku_results.v1 모델을 쓴다"""
    assert analysis_run_schemas.SkuResults is SkuResults
    assert OS.OptionOutcome.model_fields["sku_results"].annotation is SkuResults


# --- 3. 값 모양·조합 규칙 ------------------------------------------------------------------------

ID = "11111111-2222-3333-4444-555555555555"
ID2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _warehouse(code="LAX", region="WEST"):
    return {"warehouse_id": ID, "warehouse_code": code, "region_code": region}


def _delay_context(**overrides):
    body = {
        "shipment_id": ID,
        "base_date": "2026-01-10",
        "planned_3pl_inbound_date": "2026-01-12",
        "expected_3pl_inbound_date": "2026-01-20",
        "stage": "AT_SEA",
        "transport_mode": "OCEAN",
        "planned_kr_release_date": "2025-12-20",
        "trigger_shipment_schedule_snapshot_id": ID,
        "shipment_skus": [{"sku_id": ID, "sku_code": "SKU-1", "delayed_quantity": 100}],
        "destination_warehouse": _warehouse(),
        "donor_warehouse": _warehouse("NJ", "EAST"),
        "kr_origin_warehouse": _warehouse("KR", "KR"),
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_date", "2026/01/10"),
        ("base_date", 1767225600),  # 정수(유닉스 시각)
        ("base_date", datetime(2026, 1, 10)),
        ("shipment_id", ID2.upper()),
        ("shipment_id", ID2.replace("-", "")),
        ("stage", "SAILING"),
        ("transport_mode", "TRUCK"),
    ],
)
def test_value_shapes_are_rejected(field, value):
    with pytest.raises(ValidationError):
        L.DelayContext.model_validate(_delay_context(**{field: value}))


def test_valid_delay_context_round_trips_as_contract_json():
    model = L.DelayContext.model_validate(_delay_context(base_date=date(2026, 1, 10)))
    body = model.model_dump(mode="json")
    assert body["base_date"] == "2026-01-10"
    assert body["shipment_id"] == ID
    assert L.DelayContext.model_validate(body) == model


def test_unknown_key_is_rejected():
    with pytest.raises(ValidationError):
        L.DelayContext.model_validate(_delay_context(extra_key=1))


def _order(**overrides):
    body = {
        "order_id": ID,
        "order_code": "ORD-1",
        "warehouse_id": ID,
        "sales_channel": "AMAZON",
        "ordered_at": "2026-01-08T10:00:00Z",
        "promised_delivery_at": "2026-01-15T23:59:59Z",
        "lines": [],
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-15T23:59:59+09:00",
        "2026-01-15T23:59:59.123Z",
        "2026-01-15T23:59:59",
        "2026-01-15",
    ],
)
def test_timestamp_must_be_utc_seconds_z(value):
    with pytest.raises(ValidationError):
        L.OpenOrder.model_validate(_order(promised_delivery_at=value))
    assert (
        L.OpenOrder.model_validate(_order()).promised_delivery_at
        == "2026-01-15T23:59:59Z"
    )


def test_strict_int_and_bool():
    line = {
        "order_line_id": ID,
        "line_no": 1,
        "sku_id": ID,
        "sku_code": "S",
        "ordered_quantity": 2,
        "line_status": "ACTIVE",
    }
    L.OpenOrderLine.model_validate(line)
    for bad in (
        {"ordered_quantity": "2"},
        {"ordered_quantity": True},
        {"line_no": 1.0},
    ):
        with pytest.raises(ValidationError):
            L.OpenOrderLine.model_validate(line | bad)
    with pytest.raises(ValidationError):
        L.SkuAttribute.model_validate(
            {"sku_id": ID, "sku_code": "S", "emergency_air_eligible": "true"}
        )


def _path(**overrides):
    body = {
        "stockout_date": None,
        "stockout_days": 0,
        "safety_stock_breach_date": None,
        "min_gap": "-3.2500",
        "daily_deficits": [{"date": "2026-01-10", "deficit": "0.0000"}],
    }
    body.update(overrides)
    return body


def _impact_item(**overrides):
    body = {
        "sku_id": ID,
        "sku_code": "SKU-1",
        "sku_scope": "IMPACTED",
        "raw_baseline_shortage": "0.0000",
        "raw_total_shortage": "2.5000",
        "raw_increment_shortage": "2.5000",
        "baseline_prepare_quantity": 0,
        "increment_prepare_quantity": 3,
        "total_prepare_quantity": 3,
        "safety_stock_quantity": 10,
        "baseline": _path(),
        "total": _path(),
    }
    body.update(overrides)
    return body


def _impact(**overrides):
    body = {
        "items": [_impact_item()],
        "impacted_sku_codes": ["SKU-1"],
        "watch_sku_codes": [],
        "completed_status": "READY_FOR_REVIEW",
        "result_class": "FORECAST_ONLY_READY",
    }
    body.update(overrides)
    return body


def test_inventory_impact_rules():
    I.InventoryImpact.model_validate(_impact())
    I.InventoryImpactItem.model_validate(
        _impact_item(baseline=_path(min_gap=None))
    )  # inf → null (Q10)
    bad = [
        _impact(items=[_impact_item(total_prepare_quantity=4)]),  # 합계
        _impact(items=[_impact_item(sku_scope="WATCH")]),  # 영향 집합과 다름
        _impact(watch_sku_codes=["SKU-1"]),  # 두 집합에 함께
        _impact(result_class="PURE_NO_RISK"),  # analysis_runs_ck21
        _impact(completed_status="ON_HOLD"),
        _impact(
            items=[_impact_item(raw_total_shortage="-1.0000")]
        ),  # 부족 원값은 음수 없음
        _impact(items=[_impact_item(baseline=_path(min_gap="-3.25"))]),  # 소수 4자리
    ]
    for body in bad:
        with pytest.raises(ValidationError):
            I.InventoryImpact.model_validate(body)


def _risk(**overrides):
    body = {
        "order_id": ID,
        "order_code": "ORD-1",
        "sales_channel": "AMAZON",
        "ordered_at": "2026-01-08T10:00:00Z",
        "promised_delivery_at": "2026-01-15T23:59:59Z",
        "risk_attribution": "DELAY_INDUCED",
        "expected_delivery_date": "2026-01-22",
        "baseline_expected_delivery_date": "2026-01-15",
        "expected_beyond_horizon": False,
        "lines": [
            {
                "order_line_id": ID,
                "line_no": 1,
                "sku_code": "SKU-1",
                "open_quantity": 2,
                "wait_cause": None,
            }
        ],
    }
    body.update(overrides)
    return body


def _risks(risks=None, codes=None):
    return {
        "risks": [_risk()] if risks is None else risks,
        "delay_induced_sku_codes": ["SKU-1"] if codes is None else codes,
        "simulation_horizon_end": "2026-02-05",
        "expected_plan_simulation": {},
    }


def test_at_risk_order_rules():
    I.AtRiskOrders.model_validate(_risks())
    I.AtRiskOrder.model_validate(
        _risk(
            risk_attribution="PRE_EXISTING",
            baseline_expected_delivery_date="2026-01-16",
        )
    )
    bad = [
        _risk(
            expected_delivery_date="2026-01-15"
        ),  # ck2: 같은 날 23:59:59 는 약속보다 늦지 않음
        _risk(risk_attribution="PRE_EXISTING"),  # ck5: 기준선이 약속 안
        _risk(
            baseline_expected_delivery_date="2026-01-16"
        ),  # ck6: DELAY_INDUCED 인데 기준선도 늦음
        _risk(
            lines=[
                {
                    "order_line_id": ID,
                    "line_no": 1,
                    "sku_code": "S",
                    "open_quantity": 0,
                    "wait_cause": None,
                }
            ]
        ),
        _risk(lines=[]),
    ]
    for body in bad:
        with pytest.raises(ValidationError):
            I.AtRiskOrder.model_validate(body)
    with pytest.raises(ValidationError):
        I.AtRiskOrders.model_validate(_risks(codes=[]))  # 지연 유발 SKU 와 다름


def _precheck(
    option="KEEP_PLAN",
    precheck="CALCULATE",
    reason=None,
    supply="2026-01-20",
    eligible=None,
):
    return {
        "option_type": option,
        "precheck": precheck,
        "reason_code": reason,
        "supply_available_date": supply,
        "eligible_sku_codes": eligible or [],
    }


def test_precheck_rules():
    OS.OptionFeasibility.model_validate(
        {
            "options": [
                _precheck(),
                _precheck(
                    "KR_AIR_REPLENISH",
                    "NO_SUPPLY",
                    "NO_KR_ELIGIBLE_STOCK",
                    eligible=["SKU-1"],
                ),
                _precheck(
                    "OTHER_REGION_FULFILL",
                    "NEEDS_CHECK",
                    "DONOR_NO_NEXT_INBOUND",
                    supply="2026-01-10",
                ),
            ]
        }
    )
    OS.OptionPrecheck.model_validate(
        _precheck(
            "PRE_DEPARTURE_AIR", "NEEDS_CHECK", "MISSING_OPERATING_RULE", supply=None
        )
    )
    bad = [
        _precheck(reason="NO_RECOVERY_EFFECT"),  # CALCULATE 인데 사유
        _precheck(
            "PRE_DEPARTURE_AIR", "NO_SUPPLY", "NO_KR_ELIGIBLE_STOCK"
        ),  # 출항 전 항공에 없는 사유
        _precheck(
            "OTHER_REGION_FULFILL", "NO_SUPPLY", "NO_TRANSFERABLE_STOCK"
        ),  # 다른 권역은 NO_SUPPLY 없음
        _precheck(
            "PRE_DEPARTURE_AIR", "NEEDS_CHECK", "MISSING_OPERATING_RULE"
        ),  # 리드타임 없는데 공급일
        _precheck("PRE_DEPARTURE_AIR", supply=None),  # 계산하는데 공급일 없음
        _precheck(eligible=["SKU-1"]),  # KR_AIR_REPLENISH 아닌데 대상 SKU
    ]
    for body in bad:
        with pytest.raises(ValidationError):
            OS.OptionPrecheck.model_validate(body)
    for options in (
        [_precheck("OTHER_REGION_FULFILL", supply="2026-01-10"), _precheck()],  # 순서
        [_precheck()],  # OTHER_REGION_FULFILL 빠짐
        [
            _precheck(),
            _precheck(),
            _precheck("OTHER_REGION_FULFILL", supply="2026-01-10"),
        ],  # 중복
    ):
        with pytest.raises(ValidationError):
            OS.OptionFeasibility.model_validate({"options": options})


SKU_RESULTS = {
    "schema_version": "sku_results.v1",
    "calculation_contract": "T14-v1",
    "unit": "EA",
    "items": [],
}


def _line(**overrides):
    body = {
        "order_line_id": ID,
        "outcome": "NOT_RECOVERED",
        "not_recovered_reason": "PROMISE_UNREACHABLE_LEADTIME",
        "line_securement_status": "NOT_SECURED",
        "secured_quantity": 0,
        "recovered_quantity": 0,
        "expected_delivery_at": None,
        "source_warehouse_id": None,
        "supply_source": None,
    }
    body.update(overrides)
    return body


RECOVERED_LINE = _line(
    outcome="RECOVERED",
    not_recovered_reason=None,
    line_securement_status="FULLY_SECURED",
    secured_quantity=2,
    recovered_quantity=2,
    expected_delivery_at="2026-01-15T23:59:59Z",
    source_warehouse_id=ID2,
    supply_source="OTHER_REGION_WAREHOUSE",
)


def _option(
    option="OTHER_REGION_FULFILL",
    status="FEASIBLE",
    reason=None,
    agg=(1, 1, 0, 0),
    committed=0,
    outcomes=None,
    **overrides,
):
    fully, induced, blocked, not_rec = agg if agg is not None else (None,) * 4
    body = {
        "option_type": option,
        "feasibility_status": status,
        "reason_code": reason,
        "required_quantity": 3,
        "securable_quantity": None if agg is None else 2,
        "reduction_committed_total": None if agg is None else committed,
        "expected_delivery_date": "2026-01-10",
        "fully_recovered_orders": fully,
        "delay_induced_recovered_orders": induced,
        "item_secured_blocked_orders": blocked,
        "not_recovered_orders": not_rec,
        "sku_results": SKU_RESULTS,
        "outcomes": [RECOVERED_LINE] if outcomes is None else outcomes,
    }
    body.update(overrides)
    return body


def test_option_outcome_accepts_contract_cases():
    cases = [
        _option(),  # FEASIBLE: 지연 유발 회복 1
        _option(
            "KEEP_PLAN",
            agg=(0, 0, 0, 1),
            outcomes=[_line(not_recovered_reason="NO_RECOVERY_EFFECT")],
        ),
        # 판정 순서 1: 운영 기준 없음 → 집계 null·결과 없음
        _option(
            "PRE_DEPARTURE_AIR",
            "NEEDS_CHECK",
            "MISSING_OPERATING_RULE",
            agg=None,
            outcomes=[],
            expected_delivery_date=None,
        ),
        # 판정 순서 7(Q9): 대표 사유 DONOR_NO_NEXT_INBOUND → NEEDS_CHECK, 판정 가능한 값은 있음
        _option(
            "OTHER_REGION_FULFILL",
            "NEEDS_CHECK",
            "DONOR_NO_NEXT_INBOUND",
            agg=(0, 0, 0, 1),
            outcomes=[_line(not_recovered_reason="DONOR_NO_NEXT_INBOUND")],
        ),
        # 판정 순서 6: 확정 감소 1개 미만
        _option(
            "KR_AIR_REPLENISH",
            "NOT_FEASIBLE",
            "FORECAST_EFFECT_BELOW_ONE_UNIT",
            agg=(0, 0, 0, 0),
            outcomes=[],
        ),
    ]
    for body in cases:
        OS.OptionOutcome.model_validate(body)


@pytest.mark.parametrize(
    ("label", "body"),
    [
        (
            "Q9 NOT_FEASIBLE/DONOR_NO_NEXT_INBOUND (ck6)",
            _option(
                "OTHER_REGION_FULFILL",
                "NOT_FEASIBLE",
                "DONOR_NO_NEXT_INBOUND",
                agg=(0, 0, 0, 1),
                outcomes=[],
            ),
        ),
        ("FEASIBLE 인데 사유 (ck5)", _option(reason="NO_RECOVERY_EFFECT")),
        (
            "NOT_FEASIBLE 인데 사유 없음 (ck5)",
            _option(status="NOT_FEASIBLE", agg=(0, 0, 0, 0), outcomes=[]),
        ),
        (
            "KEEP_PLAN NOT_FEASIBLE (표 A)",
            _option(
                "KEEP_PLAN",
                "NOT_FEASIBLE",
                "NO_RECOVERY_EFFECT",
                agg=(0, 0, 0, 0),
                outcomes=[],
            ),
        ),
        (
            "대응안에 없는 사유 (표 A)",
            _option(
                "PRE_DEPARTURE_AIR",
                "NOT_FEASIBLE",
                "NO_KR_ELIGIBLE_STOCK",
                agg=(0, 0, 0, 0),
                outcomes=[],
            ),
        ),
        (
            "MISSING_REQUIRED_DATA 는 대응안 사유 아님 (Q7)",
            _option(
                "PRE_DEPARTURE_AIR",
                "NEEDS_CHECK",
                "MISSING_REQUIRED_DATA",
                agg=None,
                outcomes=[],
            ),
        ),
        ("집계 일부만 null (ck8)", _option(fully_recovered_orders=None)),
        (
            "집계 null 인데 확보 수량 있음",
            _option(
                "PRE_DEPARTURE_AIR",
                "NEEDS_CHECK",
                "MISSING_OPERATING_RULE",
                agg=None,
                outcomes=[],
                securable_quantity=0,
            ),
        ),
        (
            "집계 null 인데 주문 결과 있음",
            _option(
                "PRE_DEPARTURE_AIR", "NEEDS_CHECK", "MISSING_OPERATING_RULE", agg=None
            ),
        ),
        (
            "FEASIBLE 인데 효과 없음 (ck12)",
            _option(agg=(0, 0, 0, 1), committed=0, outcomes=[]),
        ),
        (
            "1개 미만인데 확정 감소 있음 (ck13)",
            _option(
                "KR_AIR_REPLENISH",
                "NOT_FEASIBLE",
                "FORECAST_EFFECT_BELOW_ONE_UNIT",
                agg=(0, 0, 0, 0),
                committed=1,
                outcomes=[],
            ),
        ),
        (
            "KEEP_PLAN 확정 감소 (ck14)",
            _option("KEEP_PLAN", agg=(0, 0, 0, 0), committed=1, outcomes=[]),
        ),
        ("지연 유발 회복 > 완전 회복 (ck10)", _option(agg=(0, 1, 0, 0), outcomes=[])),
        (
            "표 B 밖 미회복 사유",
            _option("KEEP_PLAN", agg=(0, 0, 0, 1), outcomes=[_line()]),
        ),
        ("필요 수량 음수 (ck11)", _option(required_quantity=-1)),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_option_outcome_rejects(label, body):
    with pytest.raises(ValidationError):
        OS.OptionOutcome.model_validate(body)


@pytest.mark.parametrize(
    "changes",
    [
        {"recovered_quantity": 1},  # ck3: NOT_RECOVERED 인데 회복 수량
        {
            "secured_quantity": 1,
            "line_securement_status": "PARTIALLY_SECURED",
        },  # ck5: 확보했는데 공급 창고 없음
        {"source_warehouse_id": ID2},  # ck5·ck6
        {
            "line_securement_status": "PARTIALLY_SECURED"
        },  # ck10: 확보 0 인데 NOT_SECURED 아님
        {"not_recovered_reason": None},  # ck11
        {"expected_delivery_at": "2026-01-15T23:59:59Z"},  # 배송 시각은 RECOVERED 만
    ],
)
def test_line_outcome_rejects(changes):
    OS.OrderLineOutcome.model_validate(_line())
    with pytest.raises(ValidationError):
        OS.OrderLineOutcome.model_validate(_line(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {
            "line_securement_status": "PARTIALLY_SECURED"
        },  # ck9: RECOVERED 는 FULLY_SECURED
        {"recovered_quantity": 3},  # ck4: 확보 < 회복
        {"expected_delivery_at": None},
        {"supply_source": None},  # ck6
    ],
)
def test_recovered_line_rejects(changes):
    OS.OrderLineOutcome.model_validate(RECOVERED_LINE)
    with pytest.raises(ValidationError):
        OS.OrderLineOutcome.model_validate(RECOVERED_LINE | changes)


def _rank(option, rank):
    return {
        "option_type": option,
        "recommendation_rank": rank,
        "is_recommended": rank == 1,
    }


def test_ranking_rules():
    OS.OptionRanking.model_validate(
        {
            "options": [
                _rank("KEEP_PLAN", 2),
                _rank("KR_AIR_REPLENISH", 1),
                _rank("OTHER_REGION_FULFILL", None),
            ]
        }
    )
    OS.OptionRanking.model_validate(
        {"options": [_rank("KEEP_PLAN", 1), _rank("OTHER_REGION_FULFILL", None)]}
    )
    bad = [
        [_rank("KEEP_PLAN", 1), _rank("OTHER_REGION_FULFILL", 2)],  # KEEP_PLAN 은 맨 뒤
        [_rank("KEEP_PLAN", 3), _rank("OTHER_REGION_FULFILL", 1)],  # 빠진 순위
        [_rank("KEEP_PLAN", 1), _rank("OTHER_REGION_FULFILL", 1)],  # 중복
    ]
    for options in bad:
        with pytest.raises(ValidationError):
            OS.OptionRanking.model_validate({"options": options})
    with pytest.raises(ValidationError):
        OS.OptionRank.model_validate(
            {
                "option_type": "KEEP_PLAN",
                "recommendation_rank": 2,
                "is_recommended": True,
            }
        )


# --- 4. 실제 overlay 결과 행 ---------------------------------------------------------------------

_OVERLAY_OPTIONS = text(
    r"""
    SELECT ro.response_option_id::text AS id, ro.analysis_run_id::text AS run_id,
           ro.option_type, ro.feasibility_status, ro.reason_code, ro.required_quantity, ro.securable_quantity,
           ro.reduction_committed_total, to_char(ro.expected_delivery_date, 'YYYY-MM-DD') AS expected_delivery_date,
           ro.fully_recovered_orders, ro.delay_induced_recovered_orders, ro.item_secured_blocked_orders,
           ro.not_recovered_orders, ro.sku_results
    FROM response_options ro
    """
)
_OVERLAY_OUTCOMES = text(
    r"""
    SELECT o.response_option_id::text AS option_id, a.order_line_id::text AS order_line_id, o.outcome,
           o.not_recovered_reason, o.line_securement_status, o.secured_quantity, o.recovered_quantity,
           to_char(o.expected_delivery_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24\:MI\:SS"Z"') AS expected_delivery_at,
           o.source_warehouse_id::text AS source_warehouse_id, o.supply_source
    FROM option_order_outcomes o JOIN at_risk_orders a ON a.at_risk_order_id = o.at_risk_order_id
    ORDER BY a.order_line_id
    """
)


async def test_overlay_rows_pass_t10_model(db_session):
    options = (await db_session.execute(_OVERLAY_OPTIONS)).mappings().all()
    outcomes = (await db_session.execute(_OVERLAY_OUTCOMES)).mappings().all()
    if not options:
        pytest.skip("분석 결과 행이 없는 DB 라 건너뜀")
    by_option: dict[str, list[dict]] = {}
    for row in outcomes:
        body = dict(row)
        by_option.setdefault(body.pop("option_id"), []).append(body)
    by_run: dict[str, list[OS.OptionOutcome]] = {}
    for row in options:
        body = dict(row)
        option_id, run_id = body.pop("id"), body.pop("run_id")
        body["outcomes"] = by_option.pop(option_id, [])
        by_run.setdefault(run_id, []).append(OS.OptionOutcome.model_validate(body))
    assert by_option == {}, "대응안에 붙지 않은 주문 결과"
    for run_options in by_run.values():
        run_options.sort(key=lambda o: OS.OPTION_ORDER.index(o.option_type))
        OS.OptionOutcomes(options=run_options)
    assert sum(len(v) for v in by_run.values()) == len(options)


_OVERLAY_RISKS = text(
    r"""
    SELECT a.analysis_run_id::text AS run_id, o.order_id::text AS order_id, o.order_code, o.sales_channel,
           to_char(o.ordered_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24\:MI\:SS"Z"') AS ordered_at,
           to_char(a.promised_delivery_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24\:MI\:SS"Z"') AS promised_delivery_at,
           a.risk_attribution,
           to_char(a.expected_delivery_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS expected_delivery_date,
           to_char(a.baseline_expected_delivery_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS baseline_expected_delivery_date,
           a.expected_beyond_horizon, a.order_line_id::text AS order_line_id, ol.line_no, s.sku_code,
           a.open_quantity, a.wait_cause
    FROM at_risk_orders a
    JOIN order_lines ol ON ol.order_line_id = a.order_line_id
    JOIN orders o ON o.order_id = ol.order_id
    JOIN skus s ON s.sku_id = ol.sku_id
    ORDER BY a.analysis_run_id, o.order_code, ol.line_no
    """
)
_ORDER_KEYS = (
    "order_id",
    "order_code",
    "sales_channel",
    "ordered_at",
    "promised_delivery_at",
    "risk_attribution",
    "expected_delivery_date",
    "baseline_expected_delivery_date",
    "expected_beyond_horizon",
)


async def test_overlay_risks_pass_t7_model(db_session):
    """저장된 위험 주문(상세 단위)을 주문 단위로 묶어 T7 모델에 넣는다. 저장 시각 T23:59:59Z → 배송일로 되돌린다."""
    rows = (await db_session.execute(_OVERLAY_RISKS)).mappings().all()
    if not rows:
        pytest.skip("위험 주문 행이 없는 DB 라 건너뜀")
    orders: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["run_id"], row["order_id"])
        order = orders.setdefault(key, {k: row[k] for k in _ORDER_KEYS} | {"lines": []})
        assert all(order[k] == row[k] for k in _ORDER_KEYS), (
            "같은 주문의 상세끼리 주문 값이 다름"
        )
        order["lines"].append(
            {
                k: row[k]
                for k in (
                    "order_line_id",
                    "line_no",
                    "sku_code",
                    "open_quantity",
                    "wait_cause",
                )
            }
        )
    for body in orders.values():
        I.AtRiskOrder.model_validate(body)
    # 음성: 같은 행에서 귀속을 뒤집으면 DDL 규칙(ck5·ck6)으로 거부된다
    flipped = next(iter(orders.values())) | {
        "risk_attribution": "DELAY_INDUCED"
        if next(iter(orders.values()))["risk_attribution"] == "PRE_EXISTING"
        else "PRE_EXISTING"
    }
    with pytest.raises(ValidationError):
        I.AtRiskOrder.model_validate(flipped)
