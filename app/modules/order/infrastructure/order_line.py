import uuid

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.orm import Mapped

from app.core.orm import Base, uuid_pk


class OrderLine(Base):
    """주문 상세 (SKU별 주문·출고 수량)"""

    __tablename__ = "order_lines"
    __table_args__ = (
        UniqueConstraint("company_id", "order_line_id", name="order_lines_uk1"),
        UniqueConstraint("order_id", "line_no", name="order_lines_uk2"),
        CheckConstraint("ordered_quantity > 0", name="order_lines_ck1"),
        CheckConstraint(
            "shipped_quantity BETWEEN 0 AND ordered_quantity", name="order_lines_ck2"
        ),
        CheckConstraint(
            "line_status IN ('ACTIVE','REMOVED_AT_SOURCE')", name="order_lines_ck3"
        ),
        ForeignKeyConstraint(
            ["company_id", "order_id"],
            ["orders.company_id", "orders.order_id"],
            name="fk_order_lines__order_id",
        ),
        ForeignKeyConstraint(
            ["company_id", "sku_id"],
            ["skus.company_id", "skus.sku_id"],
            name="fk_order_lines__sku_id",
        ),
    )

    order_line_id: Mapped[uuid_pk]
    company_id: Mapped[uuid.UUID]
    order_id: Mapped[uuid.UUID]
    line_no: Mapped[int]
    sku_id: Mapped[uuid.UUID]
    ordered_quantity: Mapped[int]
    shipped_quantity: Mapped[int]
    line_status: Mapped[str]
