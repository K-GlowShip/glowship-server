"""add orders dispatch date columns (D8: 원천에서 받는 출고 관련 날짜 4개)

Revision ID: 0005_orders
Revises: 0004
Create Date: 2026-11-03 16:00:00

API 명세 v6.2 4.3 GET /orders 의 securedDate·plannedDispatchDate·actualDispatchDate·expectedDeliveryDate 는
v6.2 DDL 의 orders 에 열이 없다. 명세 D8 은 서버 역산을 제안했지만, 백엔드는 ERP 원천에서 받는 방향으로 정했다
(2026-11-03, docs/order/order-list.md 3절). 수집이 채우기 전까지는 NULL 이다.
  - 열이 없을 때만 추가한다 (v6.2 DDL DB 와 0001 DB 모두에서 실행 가능). 모두 date, NULL 허용.
  - 리비전 ID 를 "0005" 가 아니라 "0005_orders" 로 둔 이유: ERD 설명 5절에서 0005(분석 열)·0006(run_code)은
    개발자 3 몫으로 예약되어 있어 같은 ID 를 쓰면 충돌한다. 둘 다 0004 를 잇는 가지가 되므로
    뒤에 병합되는 쪽이 alembic merge 로 head 를 하나로 합친다.
  - downgrade 는 네 열을 지운다. v6.2 DDL·0001 어느 쪽에도 없는 열이라 안전하다.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_orders"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = {
    "secured_date": "재고 확보일 (ERP 원천, UTC 날짜). NULL 이면 원천 미제공 또는 미확보",
    "planned_dispatch_date": "출고 예정일 (ERP 원천, UTC 날짜)",
    "actual_dispatch_date": "실제 출고일 (ERP 원천, UTC 날짜). 출고 전이면 NULL",
    "expected_delivery_date": "배송 예정일 (ERP 원천, UTC 날짜)",
}


def upgrade() -> None:
    op.execute(
        "ALTER TABLE orders "
        + ", ".join(f"ADD COLUMN IF NOT EXISTS {name} date" for name in _COLUMNS)
    )
    for name, comment in _COLUMNS.items():
        op.execute(f"COMMENT ON COLUMN orders.{name} IS '{comment}'")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE orders "
        + ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in _COLUMNS)
    )
