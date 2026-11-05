import copy
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import create_access_token
from app.main import app
from app.modules.delay_analysis.application.analysis_run_detail_service import (
    AnalysisRunDetail,
    AnalysisTargetDetail,
    oldest_finished_at,
)
from app.modules.delay_analysis.application.delay_event_query_service import (
    ShipmentDisplay,
)
from app.modules.delay_analysis.application.exception.analysis_run_query_exceptions import (
    AnalysisRunNotFoundException,
)
from app.modules.delay_analysis.dependencies import get_analysis_run_detail_service
from app.modules.delay_analysis.infrastructure.repository.analysis_run_detail_repository import (
    AnalysisRunRow,
    AtRiskLineRow,
    DataVersionRow,
    FinalDecisionRow,
    InventoryImpactRow,
    MemberDraftRow,
    OptionOutcomeRow,
    ResponseOptionRow,
)
from app.modules.delay_analysis.infrastructure.repository.delay_event_read_repository import (
    DelayEventRow,
    ShipmentLineRow,
)
from app.modules.delay_analysis.presentation.analysis_run_schemas import (
    AnalysisRunDetailResponse,
)

"""
    분석 실행 상세 (API 17) 라우터·응답 변환 단위 시험 (DB 없음)
      - 실제 DB 에 없는 상태(READY_FOR_REVIEW, 진행 중)를 가짜 저장 행으로 만들어 응답 변환을 본다.
      - 저장값과의 일치·고객사 범위는 test_analysis_run_detail_sql.py(실제 DB)가 본다.
"""

COMPANY_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()
EVENT_ID = uuid.uuid7()
SHIPMENT_ID = uuid.uuid7()
WAREHOUSE_ID = uuid.uuid7()
DONOR_ID = uuid.uuid7()
SKU_A = uuid.uuid7()
SKU_B = uuid.uuid7()
ORDER_1 = uuid.uuid7()
ORDER_2 = uuid.uuid7()
KEEP_ID = uuid.uuid7()
OTHER_ID = uuid.uuid7()
LEAD_ID = uuid.uuid7()
MEMBER_ID = uuid.uuid7()
NOW = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
HASH = "a" * 64


def _sku_results(sku_id: uuid.UUID) -> dict:
    return {
        "schema_version": "sku_results.v1",
        "calculation_contract": "T14-v1",
        "unit": "EA",
        "items": [
            {
                "sku_id": str(sku_id),
                "sku_scope": "IMPACTED",
                # 2026 H1 저장값은 모두 이 키가 있다(856건). 키가 없는 v1·v2 산출물은 응답에 null 로 붙는다(SkuResultItem 기본값)
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
                "donor": None,
            }
        ],
    }


EXPLANATION = {
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
    "run_summary": "지연 선적 OCN-1의 계획 입고일은 2026-02-11입니다 [E1].",
    "risk_summary": "지연 영향 SKU는 2개입니다 [E2].",
    "option_explanations": [{"option_type": "KEEP_PLAN", "text": "유지 [E1]."}],
    "recommendation_explanation": "추천은 다른 권역 출고입니다 [E1].",
    "warnings": [],
    "evidence_references": [
        {"id": "E1", "path": "/run/planned_3pl_inbound_date", "value": "2026-02-11"},
        {"id": "E2", "path": "/summary/k_impacted_count", "value": 2},
    ],
}

RUN = AnalysisRunRow(
    analysis_run_id=RUN_ID,
    run_code="AN-260101-001",
    delay_event_id=EVENT_ID,
    version_no=1,
    parent_analysis_run_id=None,
    is_latest=True,
    status="READY_FOR_REVIEW",
    completed_status="READY_FOR_REVIEW",
    result_class="READY_ORDER_RISK",
    status_reason=None,
    trigger_type="AUTO",
    trigger_reason="EPISODE_STARTED",
    requested_by_user_id=None,
    request_reason=None,
    requested_at=None,
    started_at=NOW,
    finished_at=NOW,
    stale_detected_at=None,
    stale_primary_reason=None,
    stale_reason_codes=None,
    input_seq=120,
    base_date=date(2026, 1, 1),
    operating_rule_set_id=uuid.uuid7(),
    operating_rule_version_no=3,
    calculation_contract_version="T14-v1",
    allocation_rule_version="D00-B-b2-v1",
    final_explanation_json=EXPLANATION,
    explanation_validation_status="PASSED",
    explanation_generated_at=NOW,
    model_name="mock/mock-template-v1",
    prompt_version="explain_ready_v1@1.0.0",
    failed_step=None,
    failure_code=None,
    attempt_count=None,
    retry_exhausted=None,
    failure_message=None,
)

EVENT = DelayEventRow(
    delay_event_id=EVENT_ID,
    episode_no=1,
    shipment_id=SHIPMENT_ID,
    stage="AT_SEA",
    planned_3pl_inbound_date=date(2026, 2, 11),
    expected_3pl_inbound_date=date(2026, 2, 14),
    detected_at=NOW,
    closed_at=None,
    close_reason=None,
    previous_shipment_schedule_snapshot_id=None,
    shipment_code="OCN-1",
    transport_mode="OCEAN",
    warehouse_id=WAREHOUSE_ID,
    warehouse_code="US-WEST-LA",
    region_code="US_WEST",
    us_port_name="LOS ANGELES, CA",
    origin_region_code="KR",
    run_id=RUN_ID,
    run_status="READY_FOR_REVIEW",
    run_result_class="READY_ORDER_RISK",
    run_code="AN-260101-001",
    run_failure_code=None,
    has_final_decision=False,
    at_risk_order_count=2,
)

TARGET = AnalysisTargetDetail(
    row=EVENT,
    shipment=ShipmentDisplay(
        route="KR → LOS ANGELES",
        representative_line=ShipmentLineRow(
            shipment_id=SHIPMENT_ID,
            line_no=1,
            sku_id=SKU_A,
            shipped_quantity=300,
            sku_code="SKU-A",
            product_name="크림",
        ),
        sku_count=2,
    ),
    delay_days=3,
)


def _impact(sku_id, sku_code) -> InventoryImpactRow:
    return InventoryImpactRow(
        sku_id=sku_id,
        sku_code=sku_code,
        product_name="크림",
        warehouse_id=WAREHOUSE_ID,
        warehouse_code="US-WEST-LA",
        sku_scope="IMPACTED",
        calculation_status="CALCULATED",
        forecast_method="ORDER_FIRST_APPROX_V1",
        available_quantity=40,
        committed_open_quantity=3,
        sales_basis_type="ORDER_DEMAND",
        sales_basis_days=28,
        sales_basis_start_date=date(2025, 12, 4),
        sales_basis_end_date=date(2025, 12, 31),
        sales_basis_event_days=0,
        sales_basis_event_names=[],
        average_daily_sales=Decimal("1.964286"),
        safety_stock_days=14,
        safety_stock_quantity=28,
        baseline_planned_3pl_inbound_date=date(2026, 2, 11),
        expected_3pl_inbound_date=date(2026, 2, 14),
        baseline_safety_stock_breach_date=None,
        baseline_stockout_date=None,
        baseline_stockout_days=0,
        baseline_shortage_quantity=0,
        safety_stock_breach_date=date(2026, 1, 20),
        stockout_date=date(2026, 2, 11),
        stockout_days=3,
        shortage_quantity=7,
    )


def _line(order_id, order_code, line_no, sku_id, attribution="DELAY_INDUCED"):
    return AtRiskLineRow(
        at_risk_order_id=uuid.uuid7(),
        order_id=order_id,
        order_code=order_code,
        order_line_id=uuid.uuid7(),
        line_no=line_no,
        sku_id=sku_id,
        sku_code="SKU-A" if sku_id == SKU_A else "SKU-B",
        open_quantity=2,
        wait_cause="DIRECT_SKU_SHORTAGE" if line_no == 1 else "BUNDLED_ORDER_WAIT",
        risk_attribution=attribution,
        promised_delivery_at=NOW,
        baseline_expected_delivery_at=None,
        expected_delivery_at=datetime(2026, 2, 16, tzinfo=UTC),
        expected_beyond_horizon=False,
        risk_reason="재고 부족으로 약속 배송일 초과",
    )


# 주문 1은 두 줄, 주문 2는 한 줄 (저장 순서: 주문 코드·줄 번호)
LINES = [
    _line(ORDER_1, "AMZ-0001", 1, SKU_A),
    _line(ORDER_1, "AMZ-0001", 2, SKU_B),
    _line(ORDER_2, "AMZ-0002", 1, SKU_A, attribution="PRE_EXISTING"),
]


def _option(option_id, option_type, rank, recommended) -> ResponseOptionRow:
    return ResponseOptionRow(
        response_option_id=option_id,
        option_type=option_type,
        feasibility_status="FEASIBLE",
        reason_code=None,
        required_quantity=7,
        securable_quantity=0 if option_type == "KEEP_PLAN" else 7,
        reduction_committed_total=0 if option_type == "KEEP_PLAN" else 6,
        expected_delivery_date=date(2026, 2, 14),
        fully_recovered_orders=0,
        delay_induced_recovered_orders=0 if option_type == "KEEP_PLAN" else 1,
        item_secured_blocked_orders=0,
        not_recovered_orders=2,
        recommendation_rank=rank,
        is_recommended=recommended,
        sku_results=_sku_results(SKU_A),
    )


OPTIONS = [
    _option(KEEP_ID, "KEEP_PLAN", 2, False),
    _option(OTHER_ID, "OTHER_REGION_FULFILL", 1, True),
]

OUTCOMES = [
    OptionOutcomeRow(
        response_option_id=OTHER_ID,
        at_risk_order_id=LINES[0].at_risk_order_id,
        outcome="RECOVERED",
        not_recovered_reason=None,
        line_securement_status="FULLY_SECURED",
        secured_quantity=2,
        recovered_quantity=2,
        expected_delivery_at=NOW,
        source_warehouse_id=DONOR_ID,
        source_warehouse_code="US-EAST-NJ",
        source_region_code="US_EAST",
        supply_source="OTHER_REGION_WAREHOUSE",
    ),
    OptionOutcomeRow(
        response_option_id=OTHER_ID,
        at_risk_order_id=LINES[2].at_risk_order_id,
        outcome="NOT_RECOVERED",
        not_recovered_reason="NO_TRANSFERABLE_STOCK",
        line_securement_status="NOT_SECURED",
        secured_quantity=0,
        recovered_quantity=0,
        expected_delivery_at=None,
        source_warehouse_id=None,
        source_warehouse_code=None,
        source_region_code=None,
        supply_source=None,
    ),
]

DATA_VERSIONS = [
    DataVersionRow(uuid.uuid7(), "ORDER", datetime(2026, 1, 1, 0, 1, tzinfo=UTC)),
    DataVersionRow(
        uuid.uuid7(), "SHIPMENT", datetime(2025, 12, 31, 23, 50, tzinfo=UTC)
    ),
    DataVersionRow(uuid.uuid7(), "INVENTORY", None),
]

DETAIL = AnalysisRunDetail(
    run=RUN,
    target=TARGET,
    data_versions=DATA_VERSIONS,
    data_basis_at=oldest_finished_at(DATA_VERSIONS),
    inventory_impacts=[_impact(SKU_A, "SKU-A"), _impact(SKU_B, "SKU-B")],
    at_risk_lines=LINES,
    response_options=OPTIONS,
    option_outcomes=OUTCOMES,
    member_drafts=[
        MemberDraftRow(
            member_draft_id=uuid.uuid7(),
            user_id=MEMBER_ID,
            display_name="이팀원",
            role_code="CLIENT_MEMBER",
            response_option_id=OTHER_ID,
            option_type="OTHER_REGION_FULFILL",
            opinion_text="다른 권역 출고 추천",
            created_at=NOW,
        )
    ],
    final_decision=None,
)


def _body(detail: AnalysisRunDetail = DETAIL) -> dict:
    return AnalysisRunDetailResponse.of(detail).model_dump(mode="json", by_alias=True)


# --- 응답 변환 ------------------------------------------------------------------------------------


def test_ready_for_review_run_has_all_bundles():
    body = _body()

    assert body["runCode"] == "AN-260101-001"
    assert body["staleReasonCodes"] == []
    assert body["target"]["route"] == "KR → LOS ANGELES"
    assert body["target"]["delayDays"] == 3
    assert body["target"]["representativeSku"]["skuCode"] == "SKU-A"
    assert body["inputBasis"]["operatingRuleVersionNo"] == 3
    assert body["inputBasis"]["baseDate"] == "2026-01-01"
    assert body["inputBasis"]["dataWarnings"] == []
    assert len(body["inventoryImpacts"]) == 2
    assert body["explanation"] == EXPLANATION
    assert body["finalDecision"] is None


def test_data_basis_at_is_oldest_finished_sync():
    """dataBasisAt = 사용한 수집 중 가장 오래된 수집 시각. 끝나지 않은 수집(None)은 빼고 본다"""
    assert _body()["inputBasis"]["dataBasisAt"] == "2025-12-31T23:50:00Z"
    assert oldest_finished_at([DATA_VERSIONS[2]]) is None
    assert oldest_finished_at([]) is None


def test_at_risk_lines_are_grouped_by_order():
    """위험 주문은 주문별로 묶고 상세 줄은 lines[] 에 둔다(D21, U12). 주문 수 = 서로 다른 주문 수"""
    orders = _body()["atRiskOrders"]

    assert [o["orderCode"] for o in orders] == ["AMZ-0001", "AMZ-0002"]
    assert [len(o["lines"]) for o in orders] == [2, 1]
    assert [line["lineNo"] for line in orders[0]["lines"]] == [1, 2]
    assert orders[0]["riskAttribution"] == "DELAY_INDUCED"
    assert orders[1]["riskAttribution"] == "PRE_EXISTING"
    assert orders[0]["lines"][1]["waitCause"] == "BUNDLED_ORDER_WAIT"
    assert "isBeyondHorizon" in orders[0]


def test_outcomes_attach_to_their_option():
    """주문별 결과는 자기 대응안에만 붙고, 결과가 없는 대응안은 빈 목록이다. 공급 창고는 창고 요약"""
    options = {o["optionType"]: o for o in _body()["responseOptions"]}

    assert options["KEEP_PLAN"]["outcomes"] == []
    outcomes = options["OTHER_REGION_FULFILL"]["outcomes"]
    assert len(outcomes) == 2
    assert outcomes[0]["sourceWarehouse"] == {
        "warehouseId": str(DONOR_ID),
        "warehouseCode": "US-EAST-NJ",
        "regionCode": "US_EAST",
    }
    assert outcomes[1]["sourceWarehouse"] is None
    assert all("reason" not in o for o in options.values())
    assert options["OTHER_REGION_FULFILL"]["skuResults"] == _sku_results(SKU_A)


def test_average_daily_sales_keeps_six_decimals():
    """averageDailySales 는 소수 둘째 자리 규칙의 예외로 저장값 6자리 그대로(응답 본문 규칙 2.2.1, 명세 C29)"""
    assert _body()["inventoryImpacts"][0]["averageDailySales"] == 1.964286


def test_decision_history_summaries():
    detail = replace(
        DETAIL,
        run=replace(RUN, status="FINALIZED"),
        final_decision=FinalDecisionRow(
            final_decision_id=uuid.uuid7(),
            user_id=LEAD_ID,
            display_name="김팀장",
            role_code="CLIENT_LEAD",
            response_option_id=OTHER_ID,
            option_type="OTHER_REGION_FULFILL",
            reason_text="회복 주문이 가장 많음",
            decided_at=NOW,
            checked_seq=130,
        ),
    )
    body = _body(detail)

    assert body["drafts"][0]["author"] == {
        "userId": str(MEMBER_ID),
        "displayName": "이팀원",
        "roleCode": "CLIENT_MEMBER",
    }
    assert body["drafts"][0]["responseOption"]["optionType"] == "OTHER_REGION_FULFILL"
    assert body["finalDecision"]["decidedBy"]["userId"] == str(LEAD_ID)
    assert body["finalDecision"]["responseOption"]["responseOptionId"] == str(OTHER_ID)


def test_in_progress_run_has_no_completion_fields():
    """분석 진행 중(DETECTED~VALIDATING_OUTPUT): 완료 상태·결과 분류·설명이 없고 결과 행도 아직 없다(명세 4.5 상태 표)"""
    detail = replace(
        DETAIL,
        run=replace(
            RUN,
            status="ANALYZING_IMPACT",
            completed_status=None,
            result_class=None,
            finished_at=None,
            base_date=None,
            final_explanation_json=None,
            explanation_validation_status=None,
            explanation_generated_at=None,
            model_name=None,
            prompt_version=None,
        ),
        inventory_impacts=[],
        at_risk_lines=[],
        response_options=[],
        option_outcomes=[],
        member_drafts=[],
    )
    body = _body(detail)

    assert body["status"] == "ANALYZING_IMPACT"
    assert body["completedStatus"] is None and body["resultClass"] is None
    assert (
        body["inventoryImpacts"]
        == body["atRiskOrders"]
        == body["responseOptions"]
        == []
    )
    assert body["explanation"] is None
    assert body["runCode"] == "AN-260101-001"


def test_stale_run_keeps_reason_codes():
    detail = replace(
        DETAIL,
        run=replace(
            RUN,
            status="STALE",
            stale_detected_at=NOW,
            stale_primary_reason="MULTIPLE_RELEVANT_CHANGES",
            stale_reason_codes=["RELEVANT_ORDER_CHANGED", "RELEVANT_INVENTORY_CHANGED"],
        ),
    )
    body = _body(detail)

    assert body["stalePrimaryReason"] == "MULTIPLE_RELEVANT_CHANGES"
    assert body["staleReasonCodes"] == [
        "RELEVANT_ORDER_CHANGED",
        "RELEVANT_INVENTORY_CHANGED",
    ]


# --- 라우터 ---------------------------------------------------------------------------------------


class FakeService:
    def __init__(self):
        self.calls: list[dict] = []

    async def get_analysis_run(self, *, actor, run_id):
        self.calls.append({"actor": actor, "run_id": run_id})
        if run_id != RUN_ID:
            raise AnalysisRunNotFoundException()
        return copy.deepcopy(DETAIL)


@pytest.fixture
def fake_service():
    service = FakeService()
    app.dependency_overrides[get_analysis_run_detail_service] = lambda: service
    yield service
    app.dependency_overrides.pop(get_analysis_run_detail_service, None)


def _cookie(role_code: str) -> dict:
    token = create_access_token(
        user_id=uuid.uuid7(),
        role_code=role_code,
        company_id=None if role_code in ("ADMIN", "OWNER") else COMPANY_ID,
        config=get_settings().jwt_config,
    )
    return {ACCESS_TOKEN_COOKIE: token}


async def _get(path: str, role: str | None = "CLIENT_MEMBER"):
    cookies = _cookie(role) if role else None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookies
    ) as client:
        return await client.get(path)


@pytest.mark.parametrize("role", ["CLIENT_MEMBER", "CLIENT_LEAD"])
async def test_client_gets_detail(fake_service, role):
    response = await _get(f"/api/v1/analysis-runs/{RUN_ID}", role=role)

    assert response.status_code == 200
    assert response.json()["runId"] == str(RUN_ID)
    call = fake_service.calls[0]
    assert call["run_id"] == RUN_ID and call["actor"].company_id == COMPANY_ID


async def test_missing_run_is_404(fake_service):
    response = await _get(f"/api/v1/analysis-runs/{uuid.uuid7()}")

    assert response.status_code == 404
    assert response.json()["code"] == "ANALYSIS_RUN_NOT_FOUND"


@pytest.mark.parametrize("role", ["ADMIN", "OWNER"])
async def test_non_client_role_is_forbidden(fake_service, role):
    response = await _get(f"/api/v1/analysis-runs/{RUN_ID}", role=role)

    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"  # 명세 C31 오류 코드 이름
    assert fake_service.calls == []


async def test_login_required(fake_service):
    response = await _get(f"/api/v1/analysis-runs/{RUN_ID}", role=None)

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"  # 명세 C31 오류 코드 이름
    assert fake_service.calls == []


async def test_invalid_run_id_is_validation_error(fake_service):
    response = await _get("/api/v1/analysis-runs/not-a-uuid")

    assert response.status_code == 422
    assert fake_service.calls == []
