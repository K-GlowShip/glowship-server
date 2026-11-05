import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import text

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.inventory.application.inventory_status_rules import (
    InventoryStatus,
    average_daily_sales,
    ceil_q4,
)
from app.modules.inventory.application.inventory_status_service import (
    InventoryStatusService,
)
from app.modules.inventory.infrastructure.repository.inventory_status_repository import (
    InventoryStatusRepository,
)

"""
    거점별 재고 현황 SQL 테스트 (실제 DB, 읽기 전용, 롤백, 명세 4.4·D9·C22)
      - 하루 판매량과 안전재고는 분석 엔진이 확정 DB 에 저장한 값(inventory_impact_results)과 비교한다.
        엔진은 실행마다 판매량 기간(sales_basis_start_date ~ end_date)·하루 판매량·안전재고를 창고·SKU별로 남겼다.
        같은 기간을 넣어 이 API 의 유효 주문 수요 합 ÷ 일수가 엔진 값과 같으면 수요 정의가 같은 것이다.
      - 안전재고 하회일·품절일은 엔진이 입고 예정·미출고 주문을 함께 넣어 계산하므로 비교하지 않는다(C22: 입고 미반영).
      - DB 가 없으면 db_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


def _tenant(company_id: uuid.UUID) -> TenantContext:
    return TenantContext(user_id=uuid.uuid7(), company_id=company_id)


async def test_빈_고객사는_빈_결과(db_session):
    result = await InventoryStatusService(
        InventoryStatusRepository(db_session)
    ).get_page(_tenant(UNKNOWN_ID))

    assert result.items == [] and result.total_count == 0
    assert result.inventory_date is None and result.operating_rule_version_no is None


async def test_하루_판매량과_안전재고가_엔진_저장값과_같다(db_session):
    rows = (
        await db_session.execute(
            text(
                """
                SELECT company_id, warehouse_id, sku_id, sales_basis_start_date, sales_basis_end_date,
                       sales_basis_days, average_daily_sales, safety_stock_days, safety_stock_quantity
                  FROM inventory_impact_results
                 WHERE sales_basis_type = 'ORDER_DEMAND' AND average_daily_sales IS NOT NULL
                 ORDER BY inventory_impact_result_id
                """
            )
        )
    ).all()
    if not rows:
        return  # 확정 데이터가 없는 DB
    repo = InventoryStatusRepository(db_session)
    stock_by_period: dict = {}  # (고객사, 창고, 기간) 마다 한 번만 읽는다
    checked = 0
    for row in rows:
        period = (
            row.company_id,
            row.warehouse_id,
            row.sales_basis_start_date,
            row.sales_basis_end_date,
        )
        if period not in stock_by_period:
            stock_by_period[period] = {
                s.sku_id: s
                for s in await repo.find_stock_rows(
                    row.company_id,
                    inventory_date=row.sales_basis_end_date,
                    demand_from=row.sales_basis_start_date,
                    demand_to=row.sales_basis_end_date,
                    warehouse_id=row.warehouse_id,
                )
            }
        stock = stock_by_period[period]
        if row.sku_id not in stock:
            continue  # 그날 재고 행이 없는 SKU
        daily_sales = average_daily_sales(
            stock[row.sku_id].total_demand, row.sales_basis_days
        )
        assert Decimal(repr(round(daily_sales, 6))) == row.average_daily_sales, row
        assert ceil_q4(daily_sales * row.safety_stock_days) == row.safety_stock_quantity
        checked += 1

    assert checked > 0


async def test_확정_DB_재고_현황은_기준일_7월_1일_운영_기준_1번으로_계산한다(
    db_session,
):
    company_id = await db_session.scalar(
        text("SELECT company_id FROM companies WHERE company_code = 'A_BEAUTY'")
    )
    if company_id is None:
        return

    result = await InventoryStatusService(
        InventoryStatusRepository(db_session)
    ).get_page(_tenant(company_id), page=PageParams(page=1, size=100))

    assert result.inventory_date == date(2026, 6, 30)
    assert (
        result.operating_rule_version_no,
        result.sales_basis_days,
        result.safety_stock_days,
    ) == (1, 28, 14)
    assert result.total_count == 66  # WH-EAST 30 + WH-WEST 30 + WH-KR 6
    statuses = [item.status for item in result.items]
    order = {InventoryStatus.RISK: 0, InventoryStatus.CAUTION: 1, InventoryStatus.OK: 2}
    assert statuses == sorted(statuses, key=order.__getitem__)
    kr = [item for item in result.items if item.warehouse_code == "WH-KR"]
    assert kr and all(
        item.average_daily_sales == 0.0 and item.status == InventoryStatus.OK
        for item in kr
    )  # 한국 창고는 주문 출고 창고가 아니라 판매량 0


async def test_창고를_거르면_그_창고_행만_나온다(db_session):
    company_id = await db_session.scalar(
        text("SELECT company_id FROM companies WHERE company_code = 'A_BEAUTY'")
    )
    if company_id is None:
        return
    warehouse_id = await db_session.scalar(
        text(
            "SELECT warehouse_id FROM warehouses WHERE company_id = :c AND warehouse_code = 'WH-WEST'"
        ),
        {"c": company_id},
    )

    result = await InventoryStatusService(
        InventoryStatusRepository(db_session)
    ).get_page(_tenant(company_id), warehouse_id=warehouse_id)

    assert result.total_count == 30
    assert {item.warehouse_code for item in result.items} == {"WH-WEST"}
