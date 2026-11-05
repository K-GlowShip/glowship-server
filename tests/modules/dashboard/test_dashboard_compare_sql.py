import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core.security.tenant import TenantContext
from app.modules.dashboard.application.dashboard_query_service import (
    DashboardQueryService,
)
from app.modules.dashboard.infrastructure.repository.dashboard_repository import (
    DashboardRepository,
)

"""
    대시보드 비교값 SQL 테스트 (실제 DB, 읽기 전용, 롤백, 명세 C22)
      - 과거 지연 선적 수는 확정 DB 의 지연 8건 이력에서 손으로 센 값과 비교한다.
          A_BEAUTY: 01-01 00:03 지연 4건 감지(실행 v1 00:05), 최종 선택 01-01 02:00·01-02 04:00, 01-07 지연 1건 추가,
                    01-08·01-16·01-22·01-29·02-14 종료
          B_COSMETICS: 01-01 감지 → 01-02 00:03 RECOVERED(v2 NO_DELAY), 01-02·01-03 감지 → 01-12·01-22 종료
      - 과거 진행 중인 선적 수: 기준 시각 T 의 값은 현재 값(current_stage)과 같고, T − 7일은 4건 → weekOverWeek +1.
      - DB 가 없으면 db_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

BASIS_AT = datetime(2026, 7, 1, 0, 4, 30, tzinfo=UTC)


async def _company(db, code: str) -> uuid.UUID | None:
    return await db.scalar(
        text("SELECT company_id FROM companies WHERE company_code = :code"),
        {"code": code},
    )


@pytest.mark.parametrize(
    ("code", "at", "expected"),
    [
        # 감지 직후, 최종 선택 전: A 4건 모두
        ("A_BEAUTY", datetime(2026, 1, 1, 1, 0, tzinfo=UTC), 4),
        # 최종 선택 2건(01-01 02:00, 01-02 04:00)이 빠짐
        ("A_BEAUTY", datetime(2026, 1, 2, 12, 0, tzinfo=UTC), 2),
        # 01-08 종료 1건이 빠지고 01-07 감지 1건(v1 FAILED)이 들어옴
        ("A_BEAUTY", datetime(2026, 1, 10, 12, 0, tzinfo=UTC), 2),
        # 01-16 종료로 1건만 남음 (02-14 종료 지연, 최신 끝난 실행 v3 STALE)
        ("A_BEAUTY", datetime(2026, 1, 20, 12, 0, tzinfo=UTC), 1),
        ("B_COSMETICS", datetime(2026, 1, 1, 1, 0, tzinfo=UTC), 1),
        # 01-01 지연은 01-02 00:03 회복 종료, 01-02 감지 1건(v1 ON_HOLD)
        ("B_COSMETICS", datetime(2026, 1, 2, 12, 0, tzinfo=UTC), 1),
        ("B_COSMETICS", datetime(2026, 1, 10, 12, 0, tzinfo=UTC), 2),
        ("B_COSMETICS", datetime(2026, 1, 20, 12, 0, tzinfo=UTC), 1),
    ],
)
async def test_과거_지연_선적_수는_그_시각의_열림_실행_최종_선택으로_센다(
    db_session, code, at, expected
):
    company_id = await _company(db_session, code)
    if company_id is None:
        return  # 확정 데이터가 없는 DB

    count = await DashboardRepository(db_session).count_delayed_shipments_at(
        company_id, at
    )

    assert count == expected


async def test_기준_시각의_진행_중_선적은_현재_값과_같고_7일_전은_4건이다(db_session):
    company_id = await _company(db_session, "A_BEAUTY")
    if company_id is None:
        return
    repo = DashboardRepository(db_session)

    at_basis = await repo.count_in_transit_shipments_at(company_id, BASIS_AT)
    week_ago = await repo.count_in_transit_shipments_at(
        company_id, datetime(2026, 6, 24, 0, 4, 30, tzinfo=UTC)
    )

    assert at_basis == await repo.count_in_transit_shipments(company_id) == 5
    assert week_ago == 4


async def test_스냅샷이_생기기_전에는_진행_중_선적이_0건이다(db_session):
    company_id = await _company(db_session, "A_BEAUTY")
    if company_id is None:
        return

    count = await DashboardRepository(db_session).count_in_transit_shipments_at(
        company_id, datetime(2025, 1, 1, tzinfo=UTC)
    )

    assert count == 0


async def test_확정_DB_요약_비교값(db_session):
    company_id = await _company(db_session, "A_BEAUTY")
    if company_id is None:
        return

    summary = await DashboardQueryService(DashboardRepository(db_session)).get_summary(
        TenantContext(user_id=uuid.uuid7(), company_id=company_id)
    )

    assert (summary.week_over_week, summary.day_over_day) == (1, 0)
    assert summary.month_over_month == 0.0  # 두 30일 모두 입고 전부 계획일 이내
