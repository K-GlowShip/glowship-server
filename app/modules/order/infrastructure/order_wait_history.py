from datetime import date as date_type

from sqlalchemy.orm import Mapped, mapped_column

from app.core.orm import Base


class OrderWaitHistory(Base):
    """
    주문 상세별 일자별 대기 원인과 보호 수량 (합성데이터 부속, aux 스키마)
      - 묶음 주문 대기(BUNDLED_ORDER_WAIT)로 예약된 수량이 어느 주문 몫인지 확인할 때 사용합니다.
      - 원본: _p4_auxiliary/wait_history.csv (UUID 대신 업무 코드로 연결, FK 없음)
    """

    __tablename__ = "wait_history"
    __table_args__ = ({"schema": "aux"},)

    company_code: Mapped[str] = mapped_column(primary_key=True)
    date: Mapped[date_type] = mapped_column(primary_key=True)
    order_code: Mapped[str] = mapped_column(primary_key=True)
    line_no: Mapped[int] = mapped_column(primary_key=True)
    warehouse_code: Mapped[str]
    sku_code: Mapped[str]
    wait_cause: Mapped[str]
    need: Mapped[int]
    protected_quantity: Mapped[int]
    wait_days: Mapped[int]
    overdue_days: Mapped[int]
