import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class InventoryImpactResult(Base):
    """분석 실행별 SKU·창고 재고 영향 (안전재고 이탈일, 품절일, 부족 수량)"""

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
    calculation_status: Mapped[str]
    available_quantity: Mapped[int]
    sales_basis_days: Mapped[int]
    average_daily_sales: Mapped[Decimal | None]
    safety_stock_days: Mapped[int]
    safety_stock_quantity: Mapped[int | None]
    expected_3pl_inbound_date: Mapped[date]
    safety_stock_breach_date: Mapped[date | None]
    stockout_date: Mapped[date | None]
    stockout_days: Mapped[int | None]
    shortage_quantity: Mapped[int | None]
