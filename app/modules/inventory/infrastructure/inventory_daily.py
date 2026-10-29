import uuid
from datetime import date

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class InventoryDaily(Base):
    """
    일자·창고·SKU별 재고
      - on_hand_quantity = reserved_quantity + available_quantity
      - reserved_quantity에는 다음 날 출고 대기분이 포함되므로 미출고 주문 수량과 함께 다시 차감하지 않습니다.
    """

    __tablename__ = "inventory_daily"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "inventory_daily_id", name="inventory_daily_uk1"
        ),
        UniqueConstraint(
            "company_id",
            "inventory_date",
            "warehouse_id",
            "sku_id",
            name="inventory_daily_uk2",
        ),
        CheckConstraint("on_hand_quantity >= 0", name="inventory_daily_ck1"),
        CheckConstraint("reserved_quantity >= 0", name="inventory_daily_ck2"),
        CheckConstraint("available_quantity >= 0", name="inventory_daily_ck3"),
        CheckConstraint("outbound_quantity >= 0", name="inventory_daily_ck4"),
        CheckConstraint(
            "on_hand_quantity = reserved_quantity + available_quantity",
            name="inventory_daily_ck5",
        ),
        CheckConstraint(
            "reserved_quantity <= on_hand_quantity", name="inventory_daily_ck6"
        ),
        CheckConstraint(
            "available_quantity <= on_hand_quantity", name="inventory_daily_ck7"
        ),
        ForeignKeyConstraint(
            ["company_id", "warehouse_id"],
            ["warehouses.company_id", "warehouses.warehouse_id"],
            name="fk_inventory_daily__warehouse_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "sku_id"],
            ["skus.company_id", "skus.sku_id"],
            name="fk_inventory_daily__sku_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "data_sync_run_id"],
            ["data_sync_runs.company_id", "data_sync_runs.data_sync_run_id"],
            name="fk_inventory_daily__data_sync_run_id",
        ),
    )

    inventory_daily_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    inventory_date: Mapped[date]
    warehouse_id: Mapped[uuid.UUID]
    sku_id: Mapped[uuid.UUID]
    on_hand_quantity: Mapped[int]
    reserved_quantity: Mapped[int]
    available_quantity: Mapped[int]
    outbound_quantity: Mapped[int]
    data_sync_run_id: Mapped[uuid.UUID]
