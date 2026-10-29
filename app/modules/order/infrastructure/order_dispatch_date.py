from datetime import date

from sqlalchemy.orm import Mapped, mapped_column

from app.core.orm import Base


class OrderDispatchDate(Base):
    """
    주문별 재고 확보일·출고 예정일·실제 출고일 (합성데이터 부속, aux 스키마)
      - OPEN 주문 중 재고를 확보하고 다음 날 출고를 기다리는 주문을 구분할 때 사용합니다.
      - 원본: _p4_auxiliary/derived_order_dispatch_dates.csv (UUID 대신 업무 코드로 연결, FK 없음)
    """

    __tablename__ = "derived_order_dispatch_dates"
    __table_args__ = ({"schema": "aux"},)

    company_code: Mapped[str] = mapped_column(primary_key=True)
    order_code: Mapped[str] = mapped_column(primary_key=True)
    warehouse_code: Mapped[str]
    customer_region: Mapped[str]
    ordered_date: Mapped[date]
    secured_date: Mapped[date | None]
    actual_dispatch_date: Mapped[date | None]
    planned_dispatch_date: Mapped[date | None]
    customer_delivery_days: Mapped[int]
    expected_delivery_date: Mapped[date | None]
    year_end_status: Mapped[str]
