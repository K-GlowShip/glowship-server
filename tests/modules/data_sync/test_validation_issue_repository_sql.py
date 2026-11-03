from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from app.modules.data_sync.application.validation.blocking_issues import (
    BlockingIssue,
    BlockingIssueService,
)
from app.modules.data_sync.infrastructure.repository.validation_issue_repository import (
    ValidationIssueRepository,
)

"""
    분석 보류 판정 SQL 테스트 (실제 DB, 읽기 전용). 확정 기준선 E9 로 확인합니다.
      - B사 재고 이슈 WMS-EWR01|SKU-215|2026-01-01 NEGATIVE_QUANTITY: 01-02 00:04:30 감지, 01-03 00:00 해소
      - E9 v1 (01-02 00:05 실행, 기준일 01-02) → ON_HOLD REQUIRED_DATA_INVALID
      - E9 v2 (01-03 02:00 재분석) → 이슈가 해소되어 보류 아님
"""

E9_KEY = "WMS-EWR01|SKU-215|2026-01-01"


async def _e9_scope(db):
    """B사(E9 이슈의 고객사)와 그 고객사의 미국 3PL 창고(도착·공여)"""
    company_id = await db.scalar(
        text(
            "SELECT company_id FROM data_validation_issues WHERE source_record_key = :k"
        ),
        {"k": E9_KEY},
    )
    if company_id is None:
        pytest.skip("확정 기준선 E9 이슈가 없는 DB")
    warehouse_ids = set(
        await db.scalars(
            text(
                "SELECT warehouse_id FROM warehouses WHERE company_id = :c AND warehouse_type = 'US_3PL'"
            ),
            {"c": company_id},
        )
    )
    return company_id, warehouse_ids


async def _find(db, confirmed_at, base_date=date(2026, 1, 2)):
    company_id, warehouse_ids = await _e9_scope(db)
    return await BlockingIssueService(ValidationIssueRepository(db)).find(
        company_id,
        confirmed_at=confirmed_at,
        base_date=base_date,
        sku_codes={"SKU-215"},
        warehouse_ids=warehouse_ids,
    )


async def test_e9_v1_is_blocked_by_open_inventory_issue(db_session):
    blocking = await _find(db_session, datetime(2026, 1, 2, 0, 5, tzinfo=UTC))

    assert blocking == BlockingIssue(E9_KEY, "NEGATIVE_QUANTITY")


@pytest.mark.parametrize(
    "confirmed_at",
    [
        datetime(2026, 1, 3, 2, 0, tzinfo=UTC),  # E9 v2: 해소된 뒤
        datetime(2026, 1, 2, 0, 4, 29, tzinfo=UTC),  # 감지 전
    ],
)
async def test_e9_not_blocked_before_detection_or_after_resolution(
    db_session, confirmed_at
):
    assert await _find(db_session, confirmed_at) is None


async def test_unmapped_warehouse_issue_does_not_block(db_session):
    """기준선 WMS-ATL01|SKU-201|2025-12-31 UNKNOWN_WAREHOUSE 는 열려 있어도 분석 창고 행이 아니라 막지 않음"""
    db = db_session
    company_id, warehouse_ids = await _e9_scope(db)

    blocking = await BlockingIssueService(ValidationIssueRepository(db)).find(
        company_id,
        confirmed_at=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        base_date=date(2026, 1, 1),
        sku_codes={"SKU-201"},
        warehouse_ids=warehouse_ids,
    )

    assert blocking is None
