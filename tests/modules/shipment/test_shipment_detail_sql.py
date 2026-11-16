import uuid

import pytest
from sqlalchemy import text

from app.core.security.tenant import TenantContext
from app.modules.shipment.application.exception.shipment_exceptions import (
    ShipmentNotFoundException,
)
from app.modules.shipment.application.shipment_detail_service import (
    ShipmentDetailService,
)
from app.modules.shipment.infrastructure.repository.shipment_repository import (
    ShipmentFilter,
    ShipmentRepository,
)

"""
    선적 상세 서비스·SQL 시험 (실제 DB, 읽기만, 롤백)
      - 개발 DB 의 선적 하나로 상세가 목록 행과 같은 칸을 같게 주는지, 상세 칸이 원천 표 값과 같은지 본다.
      - 선적이 없는 DB 면 데이터에 기대는 시험은 건너뛴다. DB 가 없으면 db_session 픽스처가 건너뛴다(tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


def _tenant(company_id: uuid.UUID) -> TenantContext:
    return TenantContext(user_id=uuid.uuid7(), company_id=company_id)


async def _sample(db) -> tuple[uuid.UUID, uuid.UUID]:
    """일정 관측이 둘 이상인 선적(이력 순서를 볼 수 있게). 없으면 건너뜀."""
    row = (
        await db.execute(
            text(
                """
                SELECT s.company_id, s.shipment_id
                  FROM shipments s
                  JOIN shipment_schedule_snapshots h
                    ON h.company_id = s.company_id AND h.shipment_id = s.shipment_id
                 GROUP BY s.company_id, s.shipment_id
                HAVING count(*) >= 2
                 ORDER BY count(*) DESC, s.shipment_id
                 LIMIT 1
                """
            )
        )
    ).first()
    if row is None:
        pytest.skip("일정 관측이 둘 이상인 선적이 없는 DB")
    return row.company_id, row.shipment_id


async def test_목록_행과_같은_칸은_같은_값이다(db_session):
    company_id, shipment_id = await _sample(db_session)
    repo = ShipmentRepository(db_session)
    from app.core.pagination import PageParams
    from app.modules.shipment.application.shipment_list_service import (
        ShipmentListService,
    )

    page = await ShipmentListService(repo).get_page(
        _tenant(company_id), page=PageParams(page=1, size=100)
    )
    listed = {item.shipment_id: item for item in page.items}
    total = page.total_count
    offset = 100
    while shipment_id not in listed and offset < total:
        more = await repo.find_page(
            company_id, ShipmentFilter(), offset=offset, limit=100
        )
        lines = await repo.find_lines(company_id, [r.shipment_id for r in more.rows])
        for r in more.rows:
            listed[r.shipment_id] = ShipmentListService._build_item(
                r, [line for line in lines if line.shipment_id == r.shipment_id]
            )
        offset += 100

    detail = await ShipmentDetailService(repo).get(_tenant(company_id), shipment_id)

    assert detail.item == listed[shipment_id]


async def test_상세_칸은_원천_표_값과_같다(db_session):
    company_id, shipment_id = await _sample(db_session)
    src = (
        await db_session.execute(
            text(
                """
                SELECT s.current_expected_port_arrival_date AS port, s.data_sync_run_id,
                       m.month, m.us_port_name, m.total_leadtime_p50, m.total_leadtime_p90,
                       m.confidence_leadtime, r.finished_at
                  FROM shipments s
                  JOIN market_leadtime_references m
                    ON m.market_leadtime_reference_id = s.market_leadtime_reference_id
                  JOIN data_sync_runs r
                    ON r.company_id = s.company_id AND r.data_sync_run_id = s.data_sync_run_id
                 WHERE s.company_id = :c AND s.shipment_id = :s
                """
            ),
            {"c": company_id, "s": shipment_id},
        )
    ).one()
    src_lines = (
        await db_session.execute(
            text(
                """
                SELECT line_no, sku_id, shipped_quantity
                  FROM shipment_lines
                 WHERE company_id = :c AND shipment_id = :s
                """
            ),
            {"c": company_id, "s": shipment_id},
        )
    ).all()
    observed = (
        await db_session.execute(
            text(
                """
                SELECT observed_at, shipment_schedule_snapshot_id, stage,
                       expected_port_arrival_date, expected_3pl_inbound_date
                  FROM shipment_schedule_snapshots
                 WHERE company_id = :c AND shipment_id = :s
                """
            ),
            {"c": company_id, "s": shipment_id},
        )
    ).all()

    detail = await ShipmentDetailService(ShipmentRepository(db_session)).get(
        _tenant(company_id), shipment_id
    )

    assert detail.expected_port_arrival_date == src.port
    assert detail.data_basis_at == src.finished_at and detail.data_basis_at is not None
    basis = detail.planned_basis
    assert (basis.month, basis.us_port_name, basis.total_leadtime_p50) == (
        src.month,
        src.us_port_name,
        src.total_leadtime_p50,
    )
    assert (basis.total_leadtime_p90, basis.confidence_leadtime) == (
        src.total_leadtime_p90,
        src.confidence_leadtime,
    )
    # 상세 줄: 전부, 줄 번호 순, 값이 원천과 같음
    assert [
        (line.line_no, line.sku_id, line.shipped_quantity) for line in detail.lines
    ] == sorted(
        (line.line_no, line.sku_id, line.shipped_quantity) for line in src_lines
    )
    assert len(detail.lines) >= 1
    # 이력: 관측 전부, 관측 시각 오래된 순(같으면 관측 ID 순), 값이 원천과 같음
    expected_history = [
        (
            o.observed_at,
            o.stage,
            o.expected_port_arrival_date,
            o.expected_3pl_inbound_date,
        )
        for o in sorted(
            observed, key=lambda o: (o.observed_at, o.shipment_schedule_snapshot_id)
        )
    ]
    assert [
        (
            h.observed_at,
            h.stage,
            h.expected_port_arrival_date,
            h.expected_3pl_inbound_date,
        )
        for h in detail.schedule_history
    ] == expected_history
    assert len(expected_history) >= 2


async def test_관측이_없는_선적은_이력이_빈_목록이다(db_session):
    row = (
        await db_session.execute(
            text(
                """
                SELECT s.company_id, s.shipment_id
                  FROM shipments s
                 WHERE NOT EXISTS (
                           SELECT 1 FROM shipment_schedule_snapshots h
                            WHERE h.company_id = s.company_id AND h.shipment_id = s.shipment_id)
                 ORDER BY s.shipment_id
                 LIMIT 1
                """
            )
        )
    ).first()
    if row is None:
        pytest.skip("일정 관측이 없는 선적이 없는 DB")

    detail = await ShipmentDetailService(ShipmentRepository(db_session)).get(
        _tenant(row.company_id), row.shipment_id
    )

    assert detail.schedule_history == []
    assert detail.item.shipment_id == row.shipment_id
    assert len(detail.lines) >= 1


async def test_다른_고객사의_선적은_404(db_session):
    company_id, shipment_id = await _sample(db_session)
    other = await db_session.scalar(
        text("SELECT company_id FROM companies WHERE company_id <> :c LIMIT 1"),
        {"c": company_id},
    )
    service = ShipmentDetailService(ShipmentRepository(db_session))

    with pytest.raises(ShipmentNotFoundException):
        await service.get(_tenant(other or UNKNOWN_ID), shipment_id)


async def test_없는_선적은_404이고_이력_SQL_도_실행된다(db_session):
    repo = ShipmentRepository(db_session)

    with pytest.raises(ShipmentNotFoundException):
        await ShipmentDetailService(repo).get(_tenant(UNKNOWN_ID), UNKNOWN_ID)
    assert await repo.find_history(UNKNOWN_ID, UNKNOWN_ID) == []
