import uuid
from datetime import date
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.orm import Base, uuid_pk


class ResponseOption(Base):
    """분석 실행별 대응안 (계획 유지, 출항 전 항공 전환, 한국 항공 보충, 다른 권역 창고 출고)"""

    __tablename__ = "response_options"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "response_option_id", name="response_options_uk1"
        ),
        UniqueConstraint("analysis_run_id", "option_type", name="response_options_uk2"),
        UniqueConstraint(
            "company_id",
            "analysis_run_id",
            "response_option_id",
            name="response_options_uk3",
        ),
        CheckConstraint(
            "option_type IN ('KEEP_PLAN','PRE_DEPARTURE_AIR','KR_AIR_REPLENISH','OTHER_REGION_FULFILL')",
            name="response_options_ck1",
        ),
        CheckConstraint(
            "feasibility_status IN ('FEASIBLE','NOT_FEASIBLE','NEEDS_CHECK')",
            name="response_options_ck2",
        ),
        CheckConstraint(
            "feasibility_status = 'FEASIBLE' OR reason IS NOT NULL",
            name="response_options_ck3",
        ),
        CheckConstraint(
            "NOT is_recommended OR feasibility_status = 'FEASIBLE'",
            name="response_options_ck4",
        ),
        # v6.2(0005): 사유 코드·주문 집계 NULL·추천 순위·SKU별 결과
        CheckConstraint(
            "(feasibility_status = 'FEASIBLE') = (reason_code IS NULL)",
            name="response_options_ck5",
        ),
        CheckConstraint(
            "feasibility_status <> 'NOT_FEASIBLE' OR reason_code IN ('ARRIVES_AFTER_DELAYED_SHIPMENT','PROMISE_UNREACHABLE_LEADTIME','NO_KR_ELIGIBLE_STOCK','NO_TRANSFERABLE_STOCK','FORECAST_EFFECT_BELOW_ONE_UNIT','NO_RECOVERY_EFFECT')",
            name="response_options_ck6",
        ),
        CheckConstraint(
            "feasibility_status <> 'NEEDS_CHECK' OR reason_code IN ('DONOR_NO_NEXT_INBOUND','MISSING_OPERATING_RULE','MISSING_REQUIRED_DATA')",
            name="response_options_ck7",
        ),
        CheckConstraint(
            "(fully_recovered_orders IS NULL) = (not_recovered_orders IS NULL) AND (fully_recovered_orders IS NULL) = (item_secured_blocked_orders IS NULL) AND (fully_recovered_orders IS NULL) = (delay_induced_recovered_orders IS NULL)",
            name="response_options_ck8",
        ),
        CheckConstraint(
            "feasibility_status <> 'FEASIBLE' OR (fully_recovered_orders IS NOT NULL AND reduction_committed_total IS NOT NULL)",
            name="response_options_ck9",
        ),
        CheckConstraint(
            "fully_recovered_orders IS NULL OR (fully_recovered_orders >= 0 AND not_recovered_orders >= 0 AND item_secured_blocked_orders BETWEEN 0 AND not_recovered_orders AND delay_induced_recovered_orders BETWEEN 0 AND fully_recovered_orders)",
            name="response_options_ck10",
        ),
        CheckConstraint(
            "required_quantity >= 0 AND COALESCE(securable_quantity, 0) >= 0 AND COALESCE(reduction_committed_total, 0) >= 0",
            name="response_options_ck11",
        ),
        CheckConstraint(
            "feasibility_status <> 'FEASIBLE' OR option_type = 'KEEP_PLAN' OR delay_induced_recovered_orders >= 1 OR reduction_committed_total >= 1",
            name="response_options_ck12",
        ),
        CheckConstraint(
            "reason_code IS DISTINCT FROM 'FORECAST_EFFECT_BELOW_ONE_UNIT' OR COALESCE(reduction_committed_total = 0 AND COALESCE(delay_induced_recovered_orders, 0) = 0, false)",
            name="response_options_ck13",
        ),
        CheckConstraint(
            "option_type <> 'KEEP_PLAN' OR (COALESCE(reduction_committed_total, 0) = 0 AND COALESCE(delay_induced_recovered_orders, 0) = 0)",
            name="response_options_ck14",
        ),
        CheckConstraint(
            "recommendation_rank IS NULL OR (recommendation_rank >= 1 AND feasibility_status = 'FEASIBLE')",
            name="response_options_ck15",
        ),
        CheckConstraint(
            "is_recommended = COALESCE(recommendation_rank = 1, false)",
            name="response_options_ck16",
        ),
        CheckConstraint(
            "COALESCE(jsonb_typeof(sku_results) = 'object' AND sku_results->>'schema_version' = 'sku_results.v1' AND sku_results->>'unit' = 'EA' AND jsonb_typeof(sku_results->'items') = 'array', false)",
            name="response_options_ck17",
        ),
        ForeignKeyConstraint(
            ["company_id", "analysis_run_id"],
            ["analysis_runs.company_id", "analysis_runs.analysis_run_id"],
            name="fk_response_options__analysis_run_id",
        ),
        Index(
            "response_options_is_recommended_uq",
            "analysis_run_id",
            unique=True,
            postgresql_where=text("is_recommended"),
        ),
        # v6.2(0005): 한 실행에서 추천 순위는 겹치지 않음
        Index(
            "response_options_rank_uq",
            "analysis_run_id",
            "recommendation_rank",
            unique=True,
            postgresql_where=text("recommendation_rank IS NOT NULL"),
        ),
    )

    response_option_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    analysis_run_id: Mapped[uuid.UUID]
    option_type: Mapped[str]
    feasibility_status: Mapped[str]
    reason_code: Mapped[str | None]
    reason: Mapped[str | None]
    required_quantity: Mapped[int]
    securable_quantity: Mapped[int | None]
    reduction_committed_total: Mapped[int | None]
    expected_delivery_date: Mapped[date | None]
    # 주문 집계 4개: NULL = 계산 안 함 (ck8)
    fully_recovered_orders: Mapped[int | None]
    delay_induced_recovered_orders: Mapped[int | None]
    item_secured_blocked_orders: Mapped[int | None]
    not_recovered_orders: Mapped[int | None]
    recommendation_rank: Mapped[int | None]
    is_recommended: Mapped[bool] = mapped_column(default=False, server_default=false())
    sku_results: Mapped[dict[str, Any]]
