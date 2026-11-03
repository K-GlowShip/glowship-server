import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from app.core.orm import Base, uuid_pk


class AtRiskOrder(Base):
    """분석 실행별 납기 위험 주문 상세"""

    __tablename__ = "at_risk_orders"
    __table_args__ = (
        UniqueConstraint("company_id", "at_risk_order_id", name="at_risk_orders_uk1"),
        UniqueConstraint("analysis_run_id", "order_line_id", name="at_risk_orders_uk2"),
        UniqueConstraint(
            "company_id",
            "analysis_run_id",
            "at_risk_order_id",
            name="at_risk_orders_uk3",
        ),
        CheckConstraint("open_quantity > 0", name="at_risk_orders_ck1"),
        CheckConstraint(
            "expected_delivery_at > promised_delivery_at", name="at_risk_orders_ck2"
        ),
        # v6.2(0005): 위험 귀속·대기 원인·기준선 배송일
        CheckConstraint(
            "risk_attribution IN ('PRE_EXISTING','DELAY_INDUCED')",
            name="at_risk_orders_ck3",
        ),
        CheckConstraint(
            "wait_cause IS NULL OR wait_cause IN ('DIRECT_SKU_SHORTAGE','BUNDLED_ORDER_WAIT')",
            name="at_risk_orders_ck4",
        ),
        CheckConstraint(
            "risk_attribution <> 'PRE_EXISTING' OR COALESCE(baseline_expected_delivery_at > promised_delivery_at, false)",
            name="at_risk_orders_ck5",
        ),
        CheckConstraint(
            "risk_attribution <> 'DELAY_INDUCED' OR baseline_expected_delivery_at IS NULL OR baseline_expected_delivery_at <= promised_delivery_at",
            name="at_risk_orders_ck6",
        ),
        ForeignKeyConstraint(
            ["company_id", "analysis_run_id"],
            ["analysis_runs.company_id", "analysis_runs.analysis_run_id"],
            name="fk_at_risk_orders__analysis_run_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "order_line_id"],
            ["order_lines.company_id", "order_lines.order_line_id"],
            name="fk_at_risk_orders__order_line_id",
        ),
    )

    at_risk_order_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    analysis_run_id: Mapped[uuid.UUID]
    order_line_id: Mapped[uuid.UUID]
    open_quantity: Mapped[int]
    promised_delivery_at: Mapped[datetime]
    expected_delivery_at: Mapped[datetime]
    baseline_expected_delivery_at: Mapped[datetime | None]
    expected_beyond_horizon: Mapped[bool] = mapped_column(
        default=False, server_default=false()
    )
    risk_attribution: Mapped[str]
    wait_cause: Mapped[str | None]
    risk_reason: Mapped[str]
