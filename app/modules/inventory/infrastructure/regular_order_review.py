from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Mapped, mapped_column

from app.core.orm import Base


class RegularOrderReview(Base):
    """
    정기 발주 검토 결과 (합성데이터 부속, aux 스키마)
      - 검토일별 목표 재고·보유·운송 중 수량과 최종 발주 수량(final)을 담습니다.
      - is_warmup 행은 2024년 준비 구간이라 수요 관련 값이 비어 있습니다.
      - 원본: _p4_auxiliary/regular_order_reviews.csv (UUID 대신 업무 코드로 연결, FK 없음)
    """

    __tablename__ = "regular_order_reviews"
    __table_args__ = ({"schema": "aux"},)

    company_code: Mapped[str] = mapped_column(primary_key=True)
    route: Mapped[str] = mapped_column(primary_key=True)
    warehouse_code: Mapped[str] = mapped_column(primary_key=True)
    sku_code: Mapped[str] = mapped_column(primary_key=True)
    review_date: Mapped[date] = mapped_column(primary_key=True)
    release_date: Mapped[date]
    arrival_date: Mapped[date]
    lead_days: Mapped[int]
    is_warmup: Mapped[bool]
    event_in_window: Mapped[bool]
    history_days: Mapped[int | None]
    base: Mapped[Decimal | None]
    demand_until_next_review: Mapped[Decimal | None]
    safety_stock: Mapped[Decimal | None]
    target: Mapped[Decimal]
    on_hand: Mapped[int | None]
    total_open_commitment: Mapped[int | None]
    reserved_at_review: Mapped[int | None]
    unreserved_commitment: Mapped[int | None]
    in_transit: Mapped[int | None]
    position: Mapped[int | None]
    raw: Mapped[Decimal]
    final: Mapped[int]
    shipment_code: Mapped[str | None]
