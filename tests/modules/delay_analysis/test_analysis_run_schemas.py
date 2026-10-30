import copy
import uuid
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from app.modules.delay_analysis.presentation.analysis_run_schemas import (
    AnalysisRunDetailResponse,
    Explanation,
    OptionOrderOutcome,
    ResponseOption,
    SkuResults,
)

RUN_ID = uuid.uuid7()
OPTION_ID = uuid.uuid7()
AT_RISK_ID = uuid.uuid7()
SKU_ID = uuid.uuid7()
DONOR_WAREHOUSE_ID = uuid.uuid7()
HASH = "a" * 64
NOW = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)


# --- 저장 JSON 예시 (데이터 팀 계약 형식, 2026 H1 overlay 값 축약) ---------------------------------


def _explanation() -> dict:
    return {
        "schema_version": "explanation.v1",
        "language": "ko",
        "analysis_run_id": str(RUN_ID),
        "decision_signature_hash": HASH,
        "model_provider": "mock",
        "model_id": "mock-template-v1",
        "prompt_template_id": "explain_ready_v1",
        "prompt_template_version": "1.0.0",
        "prompt_template_sha256": HASH,
        "generated_at": "2026-01-01T00:05:00Z",
        "retry_no": 1,
        "run_summary": "지연 선적 OCN-20251229의 계획 입고일은 2026-02-11입니다 [E1].",
        "risk_summary": "지연 영향 SKU는 9개입니다 [E2].",
        "option_explanations": [
            {
                "option_type": "KEEP_PLAN",
                "text": "현재 계획 유지: 실행 가능입니다 [E3].",
            }
        ],
        "recommendation_explanation": "추천 대응안은 현재 계획 유지입니다 [E3].",
        "warnings": [
            {
                "code": "BASELINE_SHORTAGE_EXISTS",
                "text": "계획 기준으로 이미 부족한 SKU가 있습니다.",
            }
        ],
        "evidence_references": [
            {
                "id": "E1",
                "path": "/run/planned_3pl_inbound_date",
                "value": "2026-02-11",
            },
            {"id": "E2", "path": "/summary/k_impacted_count", "value": 9},
            {
                "id": "E3",
                "path": "/options/KEEP_PLAN/feasibility_status",
                "value": "FEASIBLE",
            },
        ],
    }


def _sku_results() -> dict:
    return {
        "schema_version": "sku_results.v1",
        "calculation_contract": "T14-v1",
        "unit": "EA",
        "items": [
            {
                "sku_id": str(SKU_ID),
                "sku_scope": "IMPACTED",
                "impact_source": "FORECAST_INCREMENT",
                "evaluation_status": "EVALUATED",
                "not_estimable_reason": None,
                "raw_baseline_shortage": "0.0000",
                "raw_increment_shortage": "6.5714",
                "raw_reduction": "6.5714",
                "raw_residual_increment": "0.0000",
                "baseline_prepare_quantity": 0,
                "increment_prepare_quantity": 7,
                "reduction_committed_quantity": 6,
                "residual_prepare_quantity": 0,
                "supply_quantity": 7,
                "supply_available_date": "2026-01-08",
                "donor": {
                    "source_warehouse_id": str(DONOR_WAREHOUSE_ID),
                    "next_inbound_basis": "N2",
                    "next_inbound_date": "2026-01-21",
                    "on_hand": 84,
                    "reserved": 2,
                    "open_commitment_net": 0,
                    "safety_stock_quantity": 47,
                    "forecast_until_next_raw": "47.0000",
                    "transferable_raw": "35.0000",
                    "transferable_quantity": 35,
                    "order_layer_used_quantity": 0,
                    "forecast_channel_share": "0.2340",
                    "forecast_securable_quantity": 7,
                },
            }
        ],
    }


def _option(**over) -> dict:
    body = {
        "response_option_id": OPTION_ID,
        "option_type": "OTHER_REGION_FULFILL",
        "feasibility_status": "FEASIBLE",
        "reason_code": None,
        "reason": None,
        "required_quantity": 7,
        "securable_quantity": 7,
        "reduction_committed_total": 6,
        "expected_delivery_date": date(2026, 1, 8),
        "fully_recovered_orders": 1,
        "delay_induced_recovered_orders": 1,
        "item_secured_blocked_orders": 0,
        "not_recovered_orders": 0,
        "recommendation_rank": 1,
        "is_recommended": True,
        "sku_results": _sku_results(),
        "outcomes": [_outcome()],
    }
    body.update(over)
    return body


def _outcome(**over) -> dict:
    body = {
        "at_risk_order_id": AT_RISK_ID,
        "outcome": "RECOVERED",
        "not_recovered_reason": None,
        "line_securement_status": "FULLY_SECURED",
        "secured_quantity": 2,
        "recovered_quantity": 2,
        "expected_delivery_at": NOW,
        "source_warehouse_id": DONOR_WAREHOUSE_ID,
        "supply_source": "OTHER_REGION_WAREHOUSE",
    }
    body.update(over)
    return body


def _ready_run() -> dict:
    """선택 대기(READY_FOR_REVIEW) 실행 1건의 서비스 쪽 값 (snake_case 이름으로 만든다)"""
    return {
        "run_id": RUN_ID,
        "run_code": None,
        "version_no": 1,
        "parent_run_id": None,
        "is_latest": True,
        "status": "READY_FOR_REVIEW",
        "completed_status": "READY_FOR_REVIEW",
        "result_class": "READY_ORDER_RISK",
        "status_reason": None,
        "trigger_type": "AUTO",
        "trigger_reason": "EPISODE_STARTED",
        "requested_by": None,
        "request_reason": None,
        "requested_at": None,
        "started_at": NOW,
        "finished_at": NOW,
        "stale_detected_at": None,
        "stale_primary_reason": None,
        "stale_reason_codes": None,
        "target": {
            "delay_event_id": uuid.uuid7(),
            "episode_no": 1,
            "shipment_code": "OCN-20251229",
            "representative_sku": "수분 크림 50ml",
            "route": "KR → LOS ANGELES",
            "stage": "AT_SEA",
            "planned_3pl_inbound_date": date(2026, 2, 11),
            "expected_3pl_inbound_date": date(2026, 2, 12),
            "delay_days": 1,
        },
        "input_basis": {
            "input_seq": 120,
            "base_date": date(2026, 1, 1),
            "operating_rule_set_id": uuid.uuid7(),
            "operating_rule_version_no": 1,
            "calculation_contract_version": "T14-v1",
            "allocation_rule_version": "D00-B-b2-v1",
            "data_versions": [
                {
                    "data_sync_run_id": uuid.uuid7(),
                    "source_domain": "ORDER",
                    "finished_at": NOW,
                }
            ],
            "data_basis_at": NOW,
            "data_warnings": [],
        },
        "inventory_impacts": [
            {
                "sku_id": SKU_ID,
                "sku_code": "SKU-001",
                "product_name": "수분 크림 50ml",
                "warehouse_id": uuid.uuid7(),
                "warehouse_code": "US-WEST-LA",
                "sku_scope": "IMPACTED",
                "calculation_status": "CALCULATED",
                "forecast_method": "ORDER_FIRST_APPROX_V1",
                "available_quantity": 40,
                "committed_open_quantity": 3,
                "sales_basis_type": "ORDER_DEMAND",
                "sales_basis_days": 28,
                "sales_basis_start_date": date(2025, 12, 4),
                "sales_basis_end_date": date(2025, 12, 31),
                "sales_basis_event_days": 0,
                "sales_basis_event_names": [],
                "average_daily_sales": "1.2500",
                "safety_stock_days": 14,
                "safety_stock_quantity": 18,
                "baseline_planned_3pl_inbound_date": date(2026, 2, 11),
                "expected_3pl_inbound_date": date(2026, 2, 12),
                "baseline_safety_stock_breach_date": date(2026, 1, 20),
                "baseline_stockout_date": None,
                "baseline_stockout_days": 0,
                "baseline_shortage_quantity": 0,
                "safety_stock_breach_date": date(2026, 1, 20),
                "stockout_date": date(2026, 2, 11),
                "stockout_days": 1,
                "shortage_quantity": 7,
            }
        ],
        "at_risk_orders": [
            {
                "order_id": uuid.uuid7(),
                "order_code": "AMZ-0001",
                "risk_attribution": "DELAY_INDUCED",
                "promised_delivery_at": NOW,
                "baseline_expected_delivery_at": None,
                "expected_delivery_at": datetime(2026, 2, 15, tzinfo=UTC),
                "expected_beyond_horizon": False,
                "risk_reason": "재고 부족으로 약속 배송일 초과",
                "lines": [
                    {
                        "at_risk_order_id": AT_RISK_ID,
                        "order_line_id": uuid.uuid7(),
                        "line_no": 1,
                        "sku_id": SKU_ID,
                        "sku_code": "SKU-001",
                        "open_quantity": 2,
                        "wait_cause": "DIRECT_SKU_SHORTAGE",
                    }
                ],
            }
        ],
        "response_options": [_option()],
        "explanation": _explanation(),
        "explanation_validation_status": "PASSED",
        "explanation_generated_at": NOW,
        "model_name": "mock/mock-template-v1",
        "prompt_version": "explain_ready_v1@1.0.0",
        "failed_step": None,
        "failure_code": None,
        "attempt_count": None,
        "retry_exhausted": None,
        "failure_message": None,
        "drafts": [],
        "final_decision": None,
    }


def _dump(body: dict) -> dict:
    return AnalysisRunDetailResponse.model_validate(body).model_dump(mode="json")


# --- 응답 필드 표기 (명세 4.5, D2·D20) -------------------------------------------------------------


def test_top_level_fields_follow_spec_camel_case():
    got = _dump(_ready_run())

    # 명세 4.5 실행·STALE·AI 설명·실패 요약 묶음의 필드 이름
    spec_fields = {
        "runId", "runCode", "versionNo", "parentRunId", "isLatest", "status", "completedStatus", "resultClass",
        "statusReason", "triggerType", "triggerReason", "requestedBy", "requestReason", "requestedAt", "startedAt",
        "finishedAt", "staleDetectedAt", "stalePrimaryReason", "staleReasonCodes", "explanation",
        "explanationValidationStatus", "explanationGeneratedAt", "modelName", "promptVersion", "failedStep",
        "failureCode", "attemptCount", "retryExhausted", "failureMessage", "drafts", "finalDecision",
    }  # fmt: skip
    groups = {
        "target",
        "inputBasis",
        "inventoryImpacts",
        "atRiskOrders",
        "responseOptions",
    }
    assert set(got) == spec_fields | groups


def test_3pl_dates_use_spec_spelling():
    """to_camel 기본 변환(planned3PlInboundDate)이 아니라 명세 표기를 쓴다"""
    got = _dump(_ready_run())

    assert {"planned3plInboundDate", "expected3plInboundDate"} <= set(got["target"])
    impact = got["inventoryImpacts"][0]
    assert {"baselinePlanned3plInboundDate", "expected3plInboundDate"} <= set(impact)
    assert not any("3Pl" in key for key in [*got["target"], *impact])


def test_stored_json_keeps_snake_case_keys():
    """explanation.v1·sku_results.v1 은 저장된 키 그대로 내려간다(D20). 감싸는 필드 이름만 camelCase"""
    got = _dump(_ready_run())

    assert set(got["explanation"]) == set(_explanation())
    option = got["responseOptions"][0]
    assert "skuResults" in option and "sku_results" not in option
    item = option["skuResults"]["items"][0]
    assert set(item) == set(_sku_results()["items"][0])
    assert item["raw_increment_shortage"] == "6.5714"
    assert item["donor"]["forecast_channel_share"] == "0.2340"


def test_response_does_not_expose_internal_fields():
    """판정 서명·의존 범위·raw 부족 원값은 서버 내부 판정용이라 응답에 없다(명세 4.5)"""
    text = AnalysisRunDetailResponse.model_validate(_ready_run()).model_dump_json()

    for internal in (
        "dependencyScope",
        "decisionSignature",
        "rawShortageQuantity",
        "rawBaselineShortageQuantity",
    ):
        assert internal not in text


# --- 저장 JSON 계약 -------------------------------------------------------------------------------


def test_explanation_rejects_unknown_key():
    """지연 원인 필드는 설명 구조에 없다(AIR-006). 알 수 없는 키는 계약상 금지"""
    body = _explanation()
    body["delay_cause"] = "세관 지연"

    with pytest.raises(ValidationError):
        Explanation.model_validate(body)


def test_sku_results_reads_output_without_impact_source():
    """v1·v2 엔진 산출물에는 impact_source 키가 없다. 없으면 '기록 없음'으로 읽는다(JSON 계약 v3 0.1절)"""
    body = _sku_results()
    del body["items"][0]["impact_source"]

    assert SkuResults.model_validate(body).items[0].impact_source is None


@pytest.mark.parametrize("bad_id", [str(RUN_ID).upper(), RUN_ID.hex])
def test_stored_json_ids_must_be_lowercase_hyphenated(bad_id):
    """저장 JSON 식별자는 소문자·하이픈 UUID 문자열이다. uuid.UUID 처럼 다른 표기를 받아 바꿔 쓰지 않는다(D20)"""
    body = _explanation()
    body["analysis_run_id"] = bad_id

    with pytest.raises(ValidationError):
        Explanation.model_validate(body)


@pytest.mark.parametrize("raw", [1.5, "1.5", "-1.0000"])
def test_sku_results_rejects_non_contract_raw_value(raw):
    """raw 값은 소수 4자리 10진 문자열만 허용한다(부동소수 금지)"""
    body = _sku_results()
    body["items"][0]["raw_reduction"] = raw

    with pytest.raises(ValidationError):
        SkuResults.model_validate(body)


# --- v6.2 코드값 ----------------------------------------------------------------------------------


def test_needs_check_option_allows_null_aggregates():
    """영향 SKU 전부 판정 불가(DONOR_NO_NEXT_INBOUND)면 주문 집계는 null 이고 주문별 결과도 없다(G-1~G-3)"""
    body = _option(
        feasibility_status="NEEDS_CHECK",
        reason_code="DONOR_NO_NEXT_INBOUND",
        reason="공여 창고의 다음 입고 없음",
        securable_quantity=None,
        reduction_committed_total=None,
        expected_delivery_date=None,
        fully_recovered_orders=None,
        delay_induced_recovered_orders=None,
        item_secured_blocked_orders=None,
        not_recovered_orders=None,
        recommendation_rank=None,
        is_recommended=False,
        outcomes=[],
    )

    option = ResponseOption.model_validate(body).model_dump(mode="json")

    assert option["fullyRecoveredOrders"] is None
    assert option["notRecoveredOrders"] is None


def test_v5_codes_are_rejected():
    """v5 의 EAST_FULFILL·PARTIAL 은 v6.2 에 없다"""
    with pytest.raises(ValidationError):
        ResponseOption.model_validate(_option(option_type="EAST_FULFILL"))
    with pytest.raises(ValidationError):
        OptionOrderOutcome.model_validate(_outcome(outcome="PARTIAL"))


def test_stale_reason_codes_exclude_multiple():
    """MULTIPLE_RELEVANT_CHANGES 는 대표 사유에만 쓰고 사유 배열에는 넣지 않는다(DDL analysis_runs_ck30)"""
    body = copy.deepcopy(_ready_run())
    body.update(
        status="STALE",
        stale_detected_at=NOW,
        stale_primary_reason="MULTIPLE_RELEVANT_CHANGES",
        stale_reason_codes=["RELEVANT_ORDER_CHANGED", "RELEVANT_INVENTORY_CHANGED"],
    )
    assert _dump(body)["stalePrimaryReason"] == "MULTIPLE_RELEVANT_CHANGES"

    body["stale_reason_codes"] = ["MULTIPLE_RELEVANT_CHANGES"]
    with pytest.raises(ValidationError):
        AnalysisRunDetailResponse.model_validate(body)


def test_on_hold_run_has_failure_summary_and_empty_results():
    """ON_HOLD 는 결과 행이 없고 실패 요약만 있다(명세 4.5 상태별 표)"""
    body = copy.deepcopy(_ready_run())
    body.update(
        status="ON_HOLD",
        completed_status="ON_HOLD",
        result_class=None,
        inventory_impacts=[],
        at_risk_orders=[],
        response_options=[],
        explanation=None,
        explanation_validation_status=None,
        explanation_generated_at=None,
        model_name=None,
        prompt_version=None,
        failed_step="VALIDATING",
        failure_code="REQUIRED_DATA_INVALID",
        attempt_count=1,
        retry_exhausted=False,
        failure_message="필수 자료 검증 오류",
    )
    body["input_basis"]["base_date"] = None

    got = _dump(body)

    assert got["failedStep"] == "VALIDATING"
    assert got["responseOptions"] == [] and got["explanation"] is None
    assert got["inputBasis"]["baseDate"] is None
