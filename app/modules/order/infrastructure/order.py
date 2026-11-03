import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class Order(Base):
    """판매 채널 주문"""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("company_id", "order_id", name="orders_uk1"),
        UniqueConstraint("company_id", "order_code", name="orders_uk2"),
        CheckConstraint("customer_region IN ('WEST','EAST')", name="orders_ck1"),
        CheckConstraint("sales_channel IN ('AMAZON','SEPHORA')", name="orders_ck2"),
        CheckConstraint(
            "status IN ('OPEN','PARTIALLY_SHIPPED','SHIPPED','DELIVERED',"
            "'CANCELLED','REMOVED_AT_SOURCE')",
            name="orders_ck3",
        ),
        ForeignKeyConstraint(
            ["company_id", "warehouse_id"],
            ["warehouses.company_id", "warehouses.warehouse_id"],
            name="fk_orders__warehouse_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "data_sync_run_id"],
            ["data_sync_runs.company_id", "data_sync_runs.data_sync_run_id"],
            name="fk_orders__data_sync_run_id",
        ),
    )

    order_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    order_code: Mapped[str]
    sales_channel: Mapped[str]
    customer_region: Mapped[str]
    warehouse_id: Mapped[uuid.UUID]
    ordered_at: Mapped[datetime]
    promised_delivery_at: Mapped[datetime]
    status: Mapped[str]
    data_sync_run_id: Mapped[uuid.UUID]
