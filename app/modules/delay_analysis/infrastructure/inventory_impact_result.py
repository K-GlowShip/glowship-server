import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKeyConstraint,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.orm import Base, uuid_pk


class InventoryImpactResult(Base):
    """분석 실행별 SKU·창고 재고 영향. 기준선(계획대로 입고)과 전체(지연 반영)를 나눠 저장합니다 (v6.2)"""

    __tablename__ = "inventory_impact_results"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "inventory_impact_result_id",
            name="inventory_impact_results_uk1",
        ),
        UniqueConstraint(
            "analysis_run_id",
            "sku_id",
            "warehouse_id",
            name="inventory_impact_results_uk2",
        ),
        CheckConstraint(
            "calculation_status IN ('CALCULATED','NOT_ESTIMABLE')",
            name="inventory_impact_results_ck1",
        ),
        CheckConstraint(
            "calculation_status <> 'CALCULATED'"
            " OR (average_daily_sales IS NOT NULL AND safety_stock_quantity IS NOT NULL"
            " AND stockout_days IS NOT NULL AND shortage_quantity IS NOT NULL)",
            name="inventory_impact_results_ck2",
        ),
        # v6.2(0005): SKU 범위·예측 방식·수요 기준·기준선/전체 분리·raw 값
        CheckConstraint(
            "sku_scope IN ('IMPACTED','WATCH','SHIPMENT_ONLY')",
            name="inventory_impact_results_ck3",
        ),
        CheckConstraint(
            "forecast_method IN ('ORDER_FIRST_APPROX_V1')",
            name="inventory_impact_results_ck4",
        ),
        CheckConstraint(
            "sales_basis_type IN ('ORDER_DEMAND','OUTBOUND')",
            name="inventory_impact_results_ck5",
        ),
        CheckConstraint(
            "(sales_basis_start_date IS NULL) = (sales_basis_end_date IS NULL) AND (sales_basis_start_date IS NULL OR sales_basis_end_date - sales_basis_start_date + 1 = sales_basis_days)",
            name="inventory_impact_results_ck6",
        ),
        CheckConstraint(
            "sales_basis_event_days IS NULL OR (sales_basis_event_days BETWEEN 0 AND sales_basis_days AND (sales_basis_event_days = 0) = (COALESCE(cardinality(sales_basis_event_names), 0) = 0))",
            name="inventory_impact_results_ck7",
        ),
        CheckConstraint(
            "baseline_planned_3pl_inbound_date <= expected_3pl_inbound_date",
            name="inventory_impact_results_ck8",
        ),
        CheckConstraint(
            "COALESCE(committed_open_quantity, 0) >= 0 AND COALESCE(raw_baseline_shortage_quantity, 0) >= 0 AND COALESCE(raw_shortage_quantity, 0) >= 0 AND COALESCE(raw_increment_shortage_quantity, 0) >= 0 AND COALESCE(baseline_stockout_days, 0) >= 0 AND COALESCE(baseline_shortage_quantity, 0) >= 0 AND COALESCE(stockout_days, 0) >= 0 AND COALESCE(shortage_quantity, 0) >= 0",
            name="inventory_impact_results_ck9",
        ),
        CheckConstraint(
            "calculation_status <> 'CALCULATED' OR (committed_open_quantity IS NOT NULL AND raw_baseline_shortage_quantity IS NOT NULL AND raw_shortage_quantity IS NOT NULL AND raw_increment_shortage_quantity IS NOT NULL AND baseline_stockout_days IS NOT NULL AND baseline_shortage_quantity IS NOT NULL AND sales_basis_start_date IS NOT NULL AND sales_basis_end_date IS NOT NULL AND sales_basis_event_days IS NOT NULL)",
            name="inventory_impact_results_ck10",
        ),
        CheckConstraint(
            "calculation_status <> 'NOT_ESTIMABLE' OR (average_daily_sales IS NULL AND safety_stock_quantity IS NULL AND raw_baseline_shortage_quantity IS NULL AND raw_shortage_quantity IS NULL AND raw_increment_shortage_quantity IS NULL AND baseline_safety_stock_breach_date IS NULL AND baseline_stockout_date IS NULL AND baseline_stockout_days IS NULL AND baseline_shortage_quantity IS NULL AND safety_stock_breach_date IS NULL AND stockout_date IS NULL AND stockout_days IS NULL AND shortage_quantity IS NULL)",
            name="inventory_impact_results_ck11",
        ),
        CheckConstraint(
            "shortage_quantity IS NULL OR baseline_shortage_quantity IS NULL OR (shortage_quantity >= baseline_shortage_quantity AND stockout_days >= baseline_stockout_days)",
            name="inventory_impact_results_ck12",
        ),
        CheckConstraint(
            "baseline_stockout_date IS NULL OR (stockout_date IS NOT NULL AND stockout_date <= baseline_stockout_date)",
            name="inventory_impact_results_ck13",
        ),
        CheckConstraint(
            "baseline_safety_stock_breach_date IS NULL OR (safety_stock_breach_date IS NOT NULL AND safety_stock_breach_date <= baseline_safety_stock_breach_date)",
            name="inventory_impact_results_ck14",
        ),
        CheckConstraint(
            "calculation_status <> 'CALCULATED' OR ((stockout_date IS NULL) = (stockout_days = 0) AND (baseline_stockout_date IS NULL) = (baseline_stockout_days = 0))",
            name="inventory_impact_results_ck15",
        ),
        CheckConstraint(
            "(stockout_date IS NULL OR stockout_date < expected_3pl_inbound_date) AND (baseline_stockout_date IS NULL OR baseline_stockout_date < expected_3pl_inbound_date)",
            name="inventory_impact_results_ck16",
        ),
        CheckConstraint(
            "raw_baseline_shortage_quantity IS NULL OR COALESCE(baseline_shortage_quantity = ceil(raw_baseline_shortage_quantity) AND shortage_quantity = baseline_shortage_quantity + ceil(raw_increment_shortage_quantity), false)",
            name="inventory_impact_results_ck17",
        ),
        CheckConstraint(
            "shortage_quantity IS NULL OR baseline_shortage_quantity IS NULL OR shortage_quantity = baseline_shortage_quantity OR sku_scope = 'IMPACTED'",
            name="inventory_impact_results_ck18",
        ),
        CheckConstraint(
            "COALESCE((raw_increment_shortage_quantity IS NULL) = (raw_baseline_shortage_quantity IS NULL) AND (raw_increment_shortage_quantity IS NULL) = (raw_shortage_quantity IS NULL) AND (raw_increment_shortage_quantity IS NULL OR abs(raw_increment_shortage_quantity - greatest(raw_shortage_quantity - raw_baseline_shortage_quantity, 0)) <= 0.0001), false)",
            name="inventory_impact_results_ck19",
        ),
        ForeignKeyConstraint(
            ["company_id", "analysis_run_id"],
            ["analysis_runs.company_id", "analysis_runs.analysis_run_id"],
            name="fk_inventory_impact_results__analysis_run_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "sku_id"],
            ["skus.company_id", "skus.sku_id"],
            name="fk_inventory_impact_results__sku_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "warehouse_id"],
            ["warehouses.company_id", "warehouses.warehouse_id"],
            name="fk_inventory_impact_results__warehouse_id",
        ),
    )

    inventory_impact_result_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    analysis_run_id: Mapped[uuid.UUID]
    sku_id: Mapped[uuid.UUID]
    warehouse_id: Mapped[uuid.UUID]
    sku_scope: Mapped[str]
    calculation_status: Mapped[str]
    forecast_method: Mapped[str]
    available_quantity: Mapped[int]
    committed_open_quantity: Mapped[int | None]
    sales_basis_type: Mapped[str]
    sales_basis_days: Mapped[int]
    sales_basis_start_date: Mapped[date | None]
    sales_basis_end_date: Mapped[date | None]
    sales_basis_event_days: Mapped[int | None]
    sales_basis_event_names: Mapped[list[str] | None] = mapped_column(ARRAY(Text()))
    average_daily_sales: Mapped[Decimal | None]
    safety_stock_days: Mapped[int]
    safety_stock_quantity: Mapped[int | None]
    baseline_planned_3pl_inbound_date: Mapped[date]
    expected_3pl_inbound_date: Mapped[date]
    raw_baseline_shortage_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4)
    )
    raw_shortage_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    baseline_safety_stock_breach_date: Mapped[date | None]
    baseline_stockout_date: Mapped[date | None]
    baseline_stockout_days: Mapped[int | None]
    baseline_shortage_quantity: Mapped[int | None]
    safety_stock_breach_date: Mapped[date | None]
    stockout_date: Mapped[date | None]
    stockout_days: Mapped[int | None]
    shortage_quantity: Mapped[int | None]
    raw_increment_shortage_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 4)
    )
