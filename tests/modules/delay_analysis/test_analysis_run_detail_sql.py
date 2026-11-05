import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.security.auth import CurrentUser
from app.modules.delay_analysis.application.analysis_run_detail_service import (
    AnalysisRunDetailService,
)
from app.modules.delay_analysis.application.exception.analysis_run_query_exceptions import (
    AnalysisRunNotFoundException,
)
from app.modules.delay_analysis.infrastructure.repository.analysis_run_detail_repository import (
    AnalysisRunDetailRepository,
)
from app.modules.delay_analysis.infrastructure.repository.delay_event_read_repository import (
    DelayEventReadRepository,
)
from app.modules.delay_analysis.presentation.analysis_run_schemas import (
    AnalysisRunDetailResponse,
)

"""
    분석 실행 상세 (API 17) 실제 DB 시험 (읽기 전용, 끝나면 롤백)
      - DB 에 있는 실행을 모두 응답으로 만들고, 서비스와 따로 쓴 SQL 로 읽은 저장값과 비교합니다(GLOW-115 인수 조건).
      - 저장 JSON(explanation.v1, sku_results.v1)은 저장된 그대로 나와야 합니다(D20).
      - 2026 H1 개발 DB 에는 실행 21건(STALE·FINALIZED·FAILED·NO_DELAY·NO_RISK·ON_HOLD)이 있습니다.
        READY_FOR_REVIEW(STALE 아님)·진행 중 실행은 없어서 그 상태는 단위 시험(test_analysis_run_api.py)으로 봅니다.
"""

_RUNS = text(
    "SELECT analysis_run_id, company_id FROM analysis_runs ORDER BY company_id, run_code"
)


def _service(db) -> AnalysisRunDetailService:
    return AnalysisRunDetailService(
        db=db,
        repo=AnalysisRunDetailRepository(db),
        delay_repo=DelayEventReadRepository(db),
    )


def _actor(company_id: uuid.UUID) -> CurrentUser:
    return CurrentUser(
        user_id=uuid.uuid7(), role_code="CLIENT_MEMBER", company_id=company_id
    )


async def _all_runs(db) -> list:
    runs = (await db.execute(_RUNS)).all()
    if not runs:
        pytest.skip("분석 실행이 없는 DB")
    return runs


async def _response(db, run_id, company_id) -> dict:
    detail = await _service(db).get_analysis_run(
        actor=_actor(company_id), run_id=run_id
    )
    return AnalysisRunDetailResponse.of(detail).model_dump(mode="json", by_alias=True)


async def _one(db, sql: str, run_id) -> dict:
    return dict((await db.execute(text(sql), {"r": run_id})).one()._mapping)


async def _all(db, sql: str, run_id) -> list[dict]:
    return [dict(row._mapping) for row in await db.execute(text(sql), {"r": run_id})]


async def test_every_run_matches_stored_values(db_session):
    db = db_session
    runs = await _all_runs(db)
    statuses = set()
    for run_id, company_id in runs:
        body = await _response(db, run_id, company_id)
        stored = await _one(
            db,
            """
            SELECT r.*, r.dependency_scope->>'base_date' AS base_date_text,
                   rs.version_no AS rule_version_no, s.shipment_code, s.current_stage,
                   e.episode_no
              FROM analysis_runs r
              JOIN operating_rule_sets rs ON rs.operating_rule_set_id = r.operating_rule_set_id
              JOIN delay_events e ON e.delay_event_id = r.delay_event_id
              JOIN shipments s ON s.shipment_id = e.shipment_id
             WHERE r.analysis_run_id = :r
            """,
            run_id,
        )
        statuses.add(stored["status"])

        # 실행·STALE·실패 요약
        assert body["runId"] == str(run_id)
        assert body["runCode"] == stored["run_code"] is not None
        for key, column in [
            ("versionNo", "version_no"),
            ("isLatest", "is_latest"),
            ("status", "status"),
            ("completedStatus", "completed_status"),
            ("resultClass", "result_class"),
            ("triggerType", "trigger_type"),
            ("triggerReason", "trigger_reason"),
            ("stalePrimaryReason", "stale_primary_reason"),
            ("failedStep", "failed_step"),
            ("failureCode", "failure_code"),
            ("attemptCount", "attempt_count"),
            ("isRetryExhausted", "retry_exhausted"),
            ("failureMessage", "failure_message"),
            ("explanationValidationStatus", "explanation_validation_status"),
            ("modelName", "model_name"),
            ("promptVersion", "prompt_version"),
        ]:
            assert body[key] == stored[column], (run_id, key)
        assert body["staleReasonCodes"] == (stored["stale_reason_codes"] or [])

        # 재분석 요청자: 사용자 요약(요청자가 없으면 null)
        requester = await _all(
            db,
            "SELECT u.user_id::text AS user_id, u.display_name, u.role_code::text AS role_code"
            " FROM analysis_runs r JOIN users u ON u.user_id = r.requested_by_user_id"
            " WHERE r.analysis_run_id = :r",
            run_id,
        )
        if requester:
            assert body["requestedBy"] == {
                "userId": requester[0]["user_id"],
                "displayName": requester[0]["display_name"],
                "roleCode": requester[0]["role_code"],
            }, run_id
        else:
            assert body["requestedBy"] is None, run_id

        # 대상(선적의 현재 값)
        target = body["target"]
        assert target["delayEventId"] == str(stored["delay_event_id"])
        assert target["episodeNo"] == stored["episode_no"]
        assert target["shipment"]["shipmentCode"] == stored["shipment_code"]
        assert target["stage"] == stored["current_stage"]

        # 입력 기준
        basis = body["inputBasis"]
        assert basis["inputSeq"] == stored["input_seq"]
        assert basis["baseDate"] == stored["base_date_text"]
        assert basis["operatingRuleVersionNo"] == stored["rule_version_no"]
        assert basis["calculationContractVersion"] == "T14-v1"
        assert basis["dataWarnings"] == []
        versions = await _all(
            db,
            "SELECT v.data_sync_run_id::text AS id, d.finished_at"
            " FROM analysis_run_data_versions v"
            " JOIN data_sync_runs d ON d.data_sync_run_id = v.data_sync_run_id"
            " WHERE v.analysis_run_id = :r",
            run_id,
        )
        assert sorted(v["dataSyncRunId"] for v in basis["dataVersions"]) == sorted(
            v["id"] for v in versions
        )
        finished = [v["finished_at"] for v in versions if v["finished_at"]]
        assert (basis["dataBasisAt"] is None) == (not finished)

        # AI 설명은 저장 JSON 그대로 (D20)
        assert body["explanation"] == stored["final_explanation_json"]


async def test_result_rows_match_stored_rows(db_session):
    db = db_session
    for run_id, company_id in await _all_runs(db):
        body = await _response(db, run_id, company_id)

        impacts = await _all(
            db,
            "SELECT i.sku_id::text AS sku_id, k.sku_code, k.product_name,"
            " i.warehouse_id::text AS warehouse_id, w.warehouse_code, w.region_code,"
            " i.sku_scope, i.shortage_quantity, i.baseline_shortage_quantity, i.stockout_days,"
            " i.stockout_date::text AS stockout_date, i.average_daily_sales"
            " FROM inventory_impact_results i"
            " JOIN skus k ON k.sku_id = i.sku_id"
            " JOIN warehouses w ON w.warehouse_id = i.warehouse_id"
            " WHERE i.analysis_run_id = :r",
            run_id,
        )
        got = sorted(
            (
                (i["sku"]["skuId"], i["sku"]["skuCode"], i["sku"]["productName"]),
                (
                    i["warehouse"]["warehouseId"],
                    i["warehouse"]["warehouseCode"],
                    i["warehouse"]["regionCode"],
                ),
                i["skuScope"],
                i["shortageQuantity"],
                i["baselineShortageQuantity"],
                i["stockoutDays"],
                i["stockoutDate"],
                None
                if i["averageDailySales"] is None
                else Decimal(str(i["averageDailySales"])),
            )
            for i in body["inventoryImpacts"]
        )
        want = sorted(
            (
                (i["sku_id"], i["sku_code"], i["product_name"]),
                (i["warehouse_id"], i["warehouse_code"], i["region_code"]),
                i["sku_scope"],
                i["shortage_quantity"],
                i["baseline_shortage_quantity"],
                i["stockout_days"],
                i["stockout_date"],
                None
                if i["average_daily_sales"] is None
                else i["average_daily_sales"].normalize(),
            )
            for i in impacts
        )
        got = [g[:-1] + (None if g[-1] is None else g[-1].normalize(),) for g in got]
        assert got == want, run_id

        # 위험 주문: 주문별로 묶고(D21) 상세 줄은 빠짐없이
        lines = await _all(
            db,
            "SELECT o.at_risk_order_id::text AS id, l.order_id::text AS order_id,"
            " o.risk_attribution, o.expected_beyond_horizon,"
            " l.sku_id::text AS sku_id, k.sku_code, k.product_name"
            " FROM at_risk_orders o JOIN order_lines l ON l.order_line_id = o.order_line_id"
            " JOIN skus k ON k.sku_id = l.sku_id"
            " WHERE o.analysis_run_id = :r",
            run_id,
        )
        orders = body["atRiskOrders"]
        assert len(orders) == len({line["order_id"] for line in lines})
        assert sorted(
            (
                order["orderId"],
                line["atRiskOrderId"],
                order["riskAttribution"],
                order["isBeyondHorizon"],
                (
                    line["sku"]["skuId"],
                    line["sku"]["skuCode"],
                    line["sku"]["productName"],
                ),
            )
            for order in orders
            for line in order["lines"]
        ) == sorted(
            (
                line["order_id"],
                line["id"],
                line["risk_attribution"],
                line["expected_beyond_horizon"],
                (line["sku_id"], line["sku_code"], line["product_name"]),
            )
            for line in lines
        )

        # 대응안: sku_results 는 저장 JSON 그대로, outcomes 는 대응안별 저장 행 전부
        options = await _all(
            db,
            "SELECT response_option_id::text AS id, option_type, feasibility_status,"
            " reason_code, not_recovered_orders, is_recommended, sku_results,"
            " (SELECT count(*) FROM option_order_outcomes x"
            "   WHERE x.response_option_id = o.response_option_id) AS outcome_count,"
            " (SELECT array_agg(DISTINCT x.source_warehouse_id::text)"
            "   FROM option_order_outcomes x"
            "  WHERE x.response_option_id = o.response_option_id"
            "    AND x.source_warehouse_id IS NOT NULL) AS sources"
            " FROM response_options o WHERE analysis_run_id = :r",
            run_id,
        )
        by_id = {o["id"]: o for o in options}
        assert sorted(by_id) == sorted(
            o["responseOptionId"] for o in body["responseOptions"]
        )
        for option in body["responseOptions"]:
            stored = by_id[option["responseOptionId"]]
            assert "reason" not in option
            assert option["optionType"] == stored["option_type"]
            assert option["feasibilityStatus"] == stored["feasibility_status"]
            assert option["reasonCode"] == stored["reason_code"]
            assert option["notRecoveredOrders"] == stored["not_recovered_orders"]
            assert option["isRecommended"] == stored["is_recommended"]
            assert option["skuResults"] == stored["sku_results"]
            assert len(option["outcomes"]) == stored["outcome_count"]
            assert sorted(
                {
                    x["sourceWarehouse"]["warehouseId"]
                    for x in option["outcomes"]
                    if x["sourceWarehouse"]
                }
            ) == sorted(stored["sources"] or [])

        # 판단 이력
        drafts = await _all(
            db,
            "SELECT member_draft_id::text AS id, user_id::text AS user_id,"
            " response_option_id::text AS option_id FROM member_drafts WHERE analysis_run_id = :r",
            run_id,
        )
        assert sorted(
            (
                d["memberDraftId"],
                d["author"]["userId"],
                (d["responseOption"] or {}).get("responseOptionId"),
            )
            for d in body["drafts"]
        ) == sorted((d["id"], d["user_id"], d["option_id"]) for d in drafts)
        final = await _all(
            db,
            "SELECT final_decision_id::text AS id, user_id::text AS user_id,"
            " response_option_id::text AS option_id, checked_seq"
            " FROM final_decisions WHERE analysis_run_id = :r",
            run_id,
        )
        if final:
            decision = body["finalDecision"]
            assert decision["finalDecisionId"] == final[0]["id"]
            assert decision["decidedBy"]["userId"] == final[0]["user_id"]
            assert (
                decision["responseOption"]["responseOptionId"] == final[0]["option_id"]
            )
            assert decision["checkedSeq"] == final[0]["checked_seq"]
        else:
            assert body["finalDecision"] is None


async def test_status_table_empty_bundles(db_session):
    """명세 4.5 '상태별로 보이는 내용': 결과 행이 없는 상태는 묶음이 비고, 결과가 있는 상태는 재고 영향이 있다."""
    db = db_session
    seen = set()
    for run_id, company_id in await _all_runs(db):
        body = await _response(db, run_id, company_id)
        status, code = body["status"], body["failureCode"]
        no_rows = status in ("NO_DELAY", "ON_HOLD") or (
            status == "FAILED" and code != "EXPLANATION_GENERATION_FAILED"
        )
        if no_rows:
            assert body["inventoryImpacts"] == [], run_id
            assert body["atRiskOrders"] == [], run_id
            assert body["responseOptions"] == [], run_id
            assert body["explanation"] is None
        if status in ("ON_HOLD", "FAILED"):
            assert (
                body["failedStep"] is not None and body["isRetryExhausted"] is not None
            )
        else:
            assert body["failedStep"] is None and body["isRetryExhausted"] is None
        if body["completedStatus"] in ("READY_FOR_REVIEW", "NO_RISK"):
            assert body["inventoryImpacts"], run_id
            assert body["inputBasis"]["baseDate"] is not None
        if status == "STALE":
            assert body["stalePrimaryReason"] and body["staleReasonCodes"]
        else:
            assert body["stalePrimaryReason"] is None and body["staleReasonCodes"] == []
        if status == "FAILED" and code == "EXPLANATION_GENERATION_FAILED":
            assert body["explanationValidationStatus"] == "FAILED"
            assert body["explanation"] is None
        seen.add(status)
    total = await db.scalar(text("SELECT count(*) FROM analysis_runs"))
    if total == 21:  # 2026 H1 개발 DB: 인수 조건의 상태가 모두 있다
        assert {
            "STALE",
            "FINALIZED",
            "FAILED",
            "NO_DELAY",
            "NO_RISK",
            "ON_HOLD",
        } <= seen


async def test_other_company_and_missing_run_are_same_404(db_session):
    db = db_session
    runs = await _all_runs(db)
    service = _service(db)
    repo = AnalysisRunDetailRepository(db)
    finds = (
        repo.find_data_versions,
        repo.find_inventory_impacts,
        repo.find_at_risk_lines,
        repo.find_response_options,
        repo.find_option_outcomes,
        repo.find_member_drafts,
    )
    non_empty = {find.__name__: 0 for find in finds}
    checked = 0
    for run_id, company_id in runs:
        other = await db.scalar(
            text("SELECT company_id FROM companies WHERE company_id <> :c LIMIT 1"),
            {"c": company_id},
        )
        if other is None:
            continue
        checked += 1
        with pytest.raises(AnalysisRunNotFoundException):
            await service.get_analysis_run(actor=_actor(other), run_id=run_id)
        # 서비스의 404 는 지연 조회의 고객사 조건으로도 날 수 있으므로, 실행·결과 조회 각각의 고객사 조건을 따로 본다
        # (변이 시험: 실행 조회의 고객사 조건을 지워도 위 404 는 그대로 났다). 모든 실행을 돌아야 결과 행이 있는 실행이 들어간다.
        assert await repo.find_run(other, run_id) is None
        for find in finds:
            assert await find(other, run_id) == [], find.__name__
            non_empty[find.__name__] += bool(await find(company_id, run_id))
        assert await repo.find_final_decision(other, run_id) is None
    if checked and len(runs) == 21:
        # 2026 H1 개발 DB: 각 조회가 자기 고객사로는 행을 돌려주는 실행이 하나 이상 있어야 위 확인이 의미 있다
        assert all(non_empty.values()), non_empty

    with pytest.raises(AnalysisRunNotFoundException):
        await service.get_analysis_run(actor=_actor(runs[0][1]), run_id=uuid.uuid7())
