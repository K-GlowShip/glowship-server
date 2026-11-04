import uuid
from datetime import UTC, datetime

from sqlalchemy import text

from app.core.security.tenant import TenantContext
from app.modules.dashboard.application.dashboard_query_service import (
    DashboardQueryService,
)
from app.modules.dashboard.infrastructure.repository.dashboard_repository import (
    DashboardRepository,
)

"""
    대시보드 요약 KPI SQL 테스트 (실제 DB, 끝나면 롤백)
      - 확정 DB(2026 H1)는 지연 8건이 모두 닫혀 있고 READY_FOR_REVIEW 실행이 없어 지연 선적·위험 주문이 0이다.
        그래서 롤백되는 트랜잭션 안에서 확정 데이터를 바꿔 지연 선적·위험 주문 규칙을 확인한다.
          - 지연 8건을 모두 연다 → 최종 선택이 있는 2건, 현재 실행이 NO_DELAY 인 1건을 빼고 A_BEAUTY 3·B_COSMETICS 2
          - at_risk_orders 가 있는 실행(상세 7행·주문 3건, D12)을 현재 실행·READY_FOR_REVIEW 로 → 위험 주문 3 (D21: 행 수 7 이 아님)
      - DB 가 없으면 db_session·db_write_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def _company(db, code: str) -> uuid.UUID | None:
    return await db.scalar(
        text("SELECT company_id FROM companies WHERE company_code = :code"),
        {"code": code},
    )


def _service(db) -> DashboardQueryService:
    return DashboardQueryService(DashboardRepository(db))


def _tenant(company_id: uuid.UUID) -> TenantContext:
    return TenantContext(user_id=uuid.uuid7(), company_id=company_id)


async def test_빈_고객사는_건수_0_비율_None(db_session):
    summary = await _service(db_session).get_summary(_tenant(UNKNOWN_ID))

    assert summary.last_synced_at is None
    assert (summary.in_transit_shipment_count, summary.at_risk_order_count) == (0, 0)
    assert summary.on_time_inbound_rate is None


async def test_확정_DB_요약_현재_값(db_session):
    company_id = await _company(db_session, "A_BEAUTY")
    if company_id is None:
        return  # 확정 데이터가 없는 DB

    summary = await _service(db_session).get_summary(_tenant(company_id))

    assert summary.last_synced_at == datetime(2026, 7, 1, 0, 4, 30, tzinfo=UTC)
    assert summary.in_transit_shipment_count == 5
    assert summary.delayed_shipment_count == 0  # 지연 8건 모두 닫힘
    assert summary.on_time_inbound_rate == 1.0  # 06-01~07-01 입고 4건 모두 계획일 이내
    assert (summary.at_risk_order_count, summary.at_risk_order_rate) == (0, 0.0)


async def test_열린_지연만_세고_최종_선택과_NO_DELAY_는_뺀다(db_write_session):
    db = db_write_session
    a_company = await _company(db, "A_BEAUTY")
    b_company = await _company(db, "B_COSMETICS")
    if a_company is None or b_company is None:
        return
    await db.execute(
        text("UPDATE delay_events SET closed_at = NULL, close_reason = NULL")
    )

    a = await _service(db).get_summary(_tenant(a_company))
    b = await _service(db).get_summary(_tenant(b_company))

    assert (a.delayed_shipment_count, b.delayed_shipment_count) == (3, 2)


async def test_위험_주문은_상세가_아니라_서로_다른_주문으로_센다(db_write_session):
    db = db_write_session
    run = (
        await db.execute(
            text(
                """
                SELECT r.analysis_run_id, r.delay_event_id, r.company_id
                  FROM analysis_runs r
                 WHERE EXISTS (SELECT 1 FROM at_risk_orders a WHERE a.analysis_run_id = r.analysis_run_id)
                 LIMIT 1
                """
            )
        )
    ).one_or_none()
    if run is None:
        return
    await db.execute(
        text(
            "UPDATE analysis_runs SET is_latest = false WHERE delay_event_id = :event AND is_latest"
        ),
        {"event": run.delay_event_id},
    )
    await db.execute(
        text(
            """
            UPDATE analysis_runs
               SET is_latest = true, status = 'READY_FOR_REVIEW',
                   stale_primary_reason = NULL, stale_reason_codes = NULL,
                   stale_detected_seq = NULL, stale_detected_at = NULL
             WHERE analysis_run_id = :run
            """
        ),
        {"run": run.analysis_run_id},
    )
    line_count = await db.scalar(
        text("SELECT COUNT(*) FROM at_risk_orders WHERE analysis_run_id = :run"),
        {"run": run.analysis_run_id},
    )
    unshipped = await db.scalar(
        text(
            "SELECT COUNT(*) FROM orders WHERE company_id = :c AND status IN ('OPEN', 'PARTIALLY_SHIPPED')"
        ),
        {"c": run.company_id},
    )

    summary = await _service(db).get_summary(_tenant(run.company_id))

    assert (line_count, summary.at_risk_order_count) == (7, 3)
    assert summary.at_risk_order_rate == 3 / unshipped
