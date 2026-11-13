import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from app.core.config import APP_DB_ROLE
from app.core.database import create_app_engine
from app.core.exceptions import ForbiddenException
from app.core.security.auth import CurrentUser
from app.modules.company.infrastructure.repository.admin_assignment_repository import (
    AdminAssignmentRepository,
)
from app.modules.company.infrastructure.repository.company_repository import (
    CompanyRepository,
)
from app.modules.decision.application.decision_clock import DecisionClock
from app.modules.operating_rule.application.operating_rule_service import (
    OperatingRuleService,
)
from app.modules.operating_rule.infrastructure.repository.operating_rule_repository import (
    NewLeadTime,
    OperatingRuleRepository,
)
from tests.core.test_app_database_rls_sql import app_login  # noqa: F401
from tests.core.test_worker_database_rls_sql import _admin_engine, _login_url

"""
    운영 기준 API 를 앱 DB 역할(glowship_v4_app, RLS 적용)로 확인 (GLOW-153, NFR-005)
      - 로컬 개발 DB 는 superuser 라 RLS 를 우회한다. 운영과 같게 앱 역할에 속한 임시 로그인 계정으로 붙어,
        배정된 ADMIN 은 조회·저장이 되고(RLS 정책 app_context_allowed 의 ADMIN 분기), 배정되지 않은 ADMIN 은
        404 가 아니라 403 인지(배정을 고객사보다 먼저 확인) 본다.
      - 저장은 바깥 트랜잭션을 롤백하므로 DB 는 그대로다(서비스의 commit 은 SAVEPOINT 만 닫는다).
      - superuser 가 아니거나 DB 에 붙을 수 없으면 건너뛴다(app_login 픽스처, SKIP_DB_ROLE_TESTS=1).
"""

NOW = datetime(2026, 7, 2, 0, 3, tzinfo=UTC)


async def _assigned_admin() -> tuple[uuid.UUID, uuid.UUID, int, uuid.UUID]:
    """(ADMIN ID, 고객사 ID, 그 고객사의 최대 버전, 그 고객사의 창고 ID). superuser 연결로 읽는다"""
    admin = _admin_engine()
    try:
        async with admin.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        """
                        SELECT a.admin_user_id, a.company_id,
                               (SELECT max(s.version_no) FROM operating_rule_sets s
                                 WHERE s.company_id = a.company_id) AS max_version_no,
                               (SELECT w.warehouse_id FROM warehouses w
                                 WHERE w.company_id = a.company_id
                                 ORDER BY w.warehouse_code LIMIT 1) AS warehouse_id
                          FROM admin_assignments a
                          JOIN users u ON u.user_id = a.admin_user_id
                                          AND u.role_code = 'ADMIN' AND u.status = 'ACTIVE'
                          JOIN companies c ON c.company_id = a.company_id
                                              AND c.status = 'ACTIVE'
                         WHERE EXISTS (SELECT 1 FROM operating_rule_sets s
                                        WHERE s.company_id = a.company_id)
                         ORDER BY a.company_id, a.admin_user_id
                         LIMIT 1
                        """
                    )
                )
            ).first()
    finally:
        await admin.dispose()
    if row is None or row.warehouse_id is None:
        pytest.skip("배정된 ADMIN 과 운영 기준이 있는 고객사가 없는 DB")
    return row.admin_user_id, row.company_id, row.max_version_no, row.warehouse_id


@pytest.fixture
async def app_role_session(app_login) -> AsyncGenerator[AsyncSession]:  # noqa: F811
    """앱 역할로 고정한 연결의 세션. 끝나면 롤백한다"""
    name, password = app_login
    engine = create_app_engine(
        _login_url(name, password),
        APP_DB_ROLE,
        poolclass=NullPool,
        connect_args={"timeout": 2},
    )
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


def _service(db: AsyncSession) -> OperatingRuleService:
    return OperatingRuleService(
        db=db,
        repo=OperatingRuleRepository(db),
        company_repo=CompanyRepository(db),
        admin_assignment_repo=AdminAssignmentRepository(db),
        clock=DecisionClock.fixed(NOW),
    )


def _actor(user_id: uuid.UUID) -> CurrentUser:
    return CurrentUser(user_id=user_id, role_code="ADMIN", company_id=None)


async def test_assigned_admin_lists_and_creates_under_app_role(app_role_session):
    db = app_role_session
    admin_id, company_id, max_version_no, warehouse_id = await _assigned_admin()
    assert await db.scalar(text("SELECT current_user")) == APP_DB_ROLE
    service = _service(db)

    before = await service.list_rule_sets(actor=_actor(admin_id), company_id=company_id)
    created = await service.create_rule_set(
        actor=_actor(admin_id),
        company_id=company_id,
        effective_from=date(2026, 7, 2),
        safety_stock_days=21,
        sales_basis_days=28,
        allow_partial_fulfillment=None,
        lead_times=[
            NewLeadTime("OUTBOUND_HANDLING", warehouse_id, None, 1, "CALENDAR_DAY"),
            NewLeadTime("CUSTOMER_DELIVERY", warehouse_id, "WEST", 3, "CALENDAR_DAY"),
        ],
    )
    after = await service.list_rule_sets(actor=_actor(admin_id), company_id=company_id)

    assert before[0].row.version_no == max_version_no
    assert created.version_no == max_version_no + 1
    newest = after[0]
    assert (newest.row.version_no, newest.is_current) == (created.version_no, True)
    assert len(newest.lead_times) == 2
    # 요청자 본인 행은 users 의 self 정책으로 보인다
    assert newest.row.author_user_id == admin_id
    assert newest.row.author_role_code == "ADMIN"
    assert len(after) == len(before) + 1


async def test_unassigned_admin_is_403_not_404_under_app_role(app_role_session):
    db = app_role_session
    _, company_id, _, _ = await _assigned_admin()
    stranger = _actor(uuid.uuid7())
    service = _service(db)

    with pytest.raises(ForbiddenException):
        await service.list_rule_sets(actor=stranger, company_id=company_id)
    with pytest.raises(ForbiddenException):
        await service.create_rule_set(
            actor=stranger,
            company_id=company_id,
            effective_from=date(2026, 7, 2),
            safety_stock_days=21,
            sales_basis_days=28,
            allow_partial_fulfillment=None,
            lead_times=[],
        )


async def test_rls_hides_rule_sets_from_unassigned_context_under_app_role(
    app_role_session,
):
    """서비스 검사와 별개로 DB 가 한 번 더 막는다: 배정되지 않은 ADMIN 컨텍스트에서는 운영 기준이 0행이고 INSERT 는 거부된다"""
    db = app_role_session
    admin_id, company_id, _, _ = await _assigned_admin()
    repo = OperatingRuleRepository(db)

    await db.execute(
        text(
            "SELECT set_config('app.user_id', :u, true), set_config('app.company_id', :c, true)"
        ),
        {"u": str(uuid.uuid7()), "c": str(company_id)},
    )
    assert await repo.find_rule_sets(company_id) == []
    assert await repo.find_lead_times(company_id) == []

    await db.execute(
        text("SELECT set_config('app.user_id', :u, true)"), {"u": str(admin_id)}
    )
    assert await repo.find_rule_sets(company_id) != []
