import uuid
from datetime import UTC, date, datetime

from sqlalchemy import text

from app.core.security.tenant import TenantContext
from app.modules.dashboard.application.dashboard_query_service import (
    DashboardQueryService,
    InboundTrendDays,
)
from app.modules.dashboard.infrastructure.repository.dashboard_repository import (
    DashboardRepository,
)

"""
    대시보드 조회 SQL 테스트 (실제 DB, 읽기 전용, 롤백)
      - 빈 고객사에서도 SQL 이 실행되는지 보고, 확정 DB(2026 H1)가 있으면 실제 값을 확인합니다.
      - 확정 DB 값: 두 고객사 모두 마지막 적재 수집 완료 2026-07-01T00:04:30Z → 기준일 2026-07-01.
        A_BEAUTY 30일(06-02~07-01) 입고 예정 5건·실제 입고 4건, 7일(06-25~07-01) 예정 1건(06-28)·실제 0건.
      - DB 가 없으면 db_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def _company(db, code: str) -> uuid.UUID | None:
    return await db.scalar(
        text("SELECT company_id FROM companies WHERE company_code = :code"),
        {"code": code},
    )


async def test_빈_고객사는_기준_시각이_없고_입고_집계는_0건이다(db_session):
    repo = DashboardRepository(db_session)

    basis_at = await repo.find_basis_at(UNKNOWN_ID)
    counts = await repo.find_inbound_counts(
        UNKNOWN_ID,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 30),
        warehouse_id=UNKNOWN_ID,
    )

    assert basis_at is None
    assert counts == []


async def test_확정_DB_입고_추이는_마지막_수집_완료일까지_센다(db_session):
    company_id = await _company(db_session, "A_BEAUTY")
    if company_id is None:
        return  # 확정 데이터가 없는 DB
    repo = DashboardRepository(db_session)
    service = DashboardQueryService(repo)
    tenant = TenantContext(user_id=uuid.uuid7(), company_id=company_id)

    basis_at = await repo.find_basis_at(company_id)
    month = await service.get_inbound_trend(tenant, days=InboundTrendDays.MONTH)
    week = await service.get_inbound_trend(tenant, days=InboundTrendDays.WEEK)

    assert basis_at == datetime(2026, 7, 1, 0, 4, 30, tzinfo=UTC)
    assert (month.points[0].day, month.points[-1].day) == (
        date(2026, 6, 2),
        date(2026, 7, 1),
    )
    assert sum(p.planned_count for p in month.points) == 5
    assert sum(p.actual_count for p in month.points) == 4
    assert [
        (p.day, p.planned_count, p.actual_count)
        for p in week.points
        if p.planned_count or p.actual_count
    ] == [(date(2026, 6, 28), 1, 0)]


async def test_창고를_거르면_그_창고의_선적만_센다(db_session):
    company_id = await _company(db_session, "A_BEAUTY")
    if company_id is None:
        return
    rows = await db_session.execute(
        text(
            """
            SELECT warehouse_id, COUNT(*) AS planned
              FROM shipments
             WHERE company_id = :company_id
               AND planned_3pl_inbound_date BETWEEN '2026-04-03' AND '2026-07-01'
             GROUP BY warehouse_id
            """
        ),
        {"company_id": company_id},
    )
    expected = {row.warehouse_id: row.planned for row in rows}
    tenant = TenantContext(user_id=uuid.uuid7(), company_id=company_id)
    service = DashboardQueryService(DashboardRepository(db_session))

    for warehouse_id, planned in expected.items():
        result = await service.get_inbound_trend(
            tenant, days=InboundTrendDays.QUARTER, warehouse_id=warehouse_id
        )
        assert sum(p.planned_count for p in result.points) == planned


async def test_기준_시각은_완료_시각이_아니라_적용_순번이_가장_큰_수집으로_고른다(
    db_write_session,
):
    """C32: 적용 순번이 더 작은 수집의 완료 시각이 더 늦어도 T 는 순번 최대 수집의 finished_at 이다 (롤백)."""
    db = db_write_session
    company_id = await _company(db, "A_BEAUTY")
    if company_id is None:
        return  # 확정 데이터가 없는 DB
    applied = (
        await db.execute(
            text(
                "SELECT data_sync_run_id, finished_at FROM data_sync_runs"
                " WHERE company_id = :company_id AND applied_seq IS NOT NULL"
                " ORDER BY applied_seq DESC LIMIT 2"
            ),
            {"company_id": company_id},
        )
    ).all()
    (_, last_finished_at), (earlier_run_id, _) = applied
    await db.execute(
        text(
            "UPDATE data_sync_runs SET finished_at = :late"
            " WHERE company_id = :company_id AND data_sync_run_id = :run_id"
        ),
        {
            "late": datetime(2026, 7, 2, tzinfo=UTC),
            "company_id": company_id,
            "run_id": earlier_run_id,
        },
    )

    basis_at = await DashboardRepository(db).find_basis_at(company_id)

    assert basis_at == last_finished_at
