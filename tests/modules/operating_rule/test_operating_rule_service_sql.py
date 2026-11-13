import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text

from app.core.exceptions import ForbiddenException
from app.core.security.auth import CurrentUser
from app.modules.company.infrastructure.repository.admin_assignment_repository import (
    AdminAssignmentRepository,
)
from app.modules.company.infrastructure.repository.company_repository import (
    CompanyRepository,
)
from app.modules.decision.application.decision_clock import DecisionClock
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
    DecisionException,
)
from app.modules.operating_rule.application.exception.operating_rule_exceptions import (
    InvalidLeadTimeException,
)
from app.modules.operating_rule.application.operating_rule_service import (
    OperatingRuleService,
)
from app.modules.operating_rule.infrastructure.repository.operating_rule_repository import (
    NewLeadTime,
    OperatingRuleRepository,
)
from tests.modules.decision.test_final_decision_service_sql import (
    NOW as DECISION_NOW,
)
from tests.modules.decision.test_final_decision_service_sql import (
    _decide,
    _ready_run,
)

"""
    운영 기준 서비스 SQL 테스트 (API 4·5, GLOW-153, 실제 DB, 끝나면 롤백)
      - 확정 DB 의 배정된 ADMIN 과 그 고객사(운영 기준 v1)를 씁니다. 없으면 건너뜁니다.
      - 새 버전 저장 뒤 기존 버전이 그대로인지, 다른 고객사와 섞이지 않는지, 배정되지 않은 ADMIN 이 거부되는지,
        적용 시작일이 지난 새 버전이 있으면 이전 버전으로 분석한 실행의 최종 선택이 OPERATING_RULE_CHANGED 로 거부되는지 확인합니다.
"""

NOW = datetime(2026, 7, 2, 0, 3, tzinfo=UTC)


async def _assigned(db, company_id: uuid.UUID | None = None):
    """배정된 ADMIN 과 그 고객사(ACTIVE, 운영 기준 있음). company_id 를 주면 그 고객사에 배정된 ADMIN"""
    row = (
        await db.execute(
            text(
                """
                SELECT a.admin_user_id, a.company_id,
                       (SELECT max(s.version_no) FROM operating_rule_sets s
                         WHERE s.company_id = a.company_id) AS max_version_no
                  FROM admin_assignments a
                  JOIN users u ON u.user_id = a.admin_user_id
                                  AND u.role_code = 'ADMIN' AND u.status = 'ACTIVE'
                  JOIN companies c ON c.company_id = a.company_id AND c.status = 'ACTIVE'
                 WHERE EXISTS (SELECT 1 FROM operating_rule_sets s
                                WHERE s.company_id = a.company_id)
                   AND (CAST(:company_id AS uuid) IS NULL
                        OR a.company_id = CAST(:company_id AS uuid))
                 ORDER BY a.company_id, a.admin_user_id
                 LIMIT 1
                """
            ),
            {"company_id": None if company_id is None else str(company_id)},
        )
    ).first()
    if row is None:
        pytest.skip("배정된 ADMIN 과 운영 기준이 있는 고객사가 없는 DB")
    return row


def _actor(user_id: uuid.UUID) -> CurrentUser:
    return CurrentUser(user_id=user_id, role_code="ADMIN", company_id=None)


def _service(db, *, now=NOW, evicted: list | None = None) -> OperatingRuleService:
    return OperatingRuleService(
        db=db,
        repo=OperatingRuleRepository(db),
        company_repo=CompanyRepository(db),
        admin_assignment_repo=AdminAssignmentRepository(db),
        clock=DecisionClock.fixed(now),
        on_saved=None if evicted is None else evicted.append,
    )


async def _current_lead_times(db, company_id) -> list[NewLeadTime]:
    """고객사의 가장 높은 버전 리드타임을 그대로 옮긴 입력 (새 버전이 분석에 쓰일 수 있는 구성)"""
    rows = await db.execute(
        text(
            """
            SELECT lt.lead_time_type, lt.warehouse_id, lt.to_region_code, lt.days, lt.day_basis
              FROM operating_lead_times lt
              JOIN operating_rule_sets s
                ON s.company_id = lt.company_id
               AND s.operating_rule_set_id = lt.operating_rule_set_id
             WHERE lt.company_id = :c
               AND s.version_no = (SELECT max(version_no) FROM operating_rule_sets
                                    WHERE company_id = :c)
             ORDER BY lt.lead_time_type, lt.warehouse_id, lt.to_region_code
            """
        ),
        {"c": company_id},
    )
    return [NewLeadTime(*row) for row in rows]


async def _create(db, row, *, effective_from=date(2026, 7, 2), lead_times=None, **kw):
    if lead_times is None:
        lead_times = await _current_lead_times(db, row.company_id)
    return await _service(db, **kw).create_rule_set(
        actor=_actor(row.admin_user_id),
        company_id=row.company_id,
        effective_from=effective_from,
        safety_stock_days=21,
        sales_basis_days=28,
        allow_partial_fulfillment=True,
        lead_times=lead_times,
    )


async def test_list_returns_versions_with_lead_times_and_one_current(db_session):
    db = db_session
    row = await _assigned(db)

    details = await _service(db).list_rule_sets(
        actor=_actor(row.admin_user_id), company_id=row.company_id
    )

    versions = [d.row.version_no for d in details]
    assert versions == sorted(versions, reverse=True)
    assert versions[0] == row.max_version_no
    assert [d.is_current for d in details].count(True) == 1
    expected = await db.scalar(
        text("SELECT count(*) FROM operating_lead_times WHERE company_id = :c"),
        {"c": row.company_id},
    )
    assert sum(len(d.lead_times) for d in details) == expected > 0
    assert all(
        lt.operating_rule_set_id == d.row.operating_rule_set_id
        for d in details
        for lt in d.lead_times
    )


async def test_create_adds_next_version_and_keeps_existing_versions(db_write_session):
    db = db_write_session
    row = await _assigned(db)
    snapshot_sql = text(
        """
        SELECT s.operating_rule_set_id, s.version_no, s.effective_from, s.safety_stock_days,
               (SELECT count(*) FROM operating_lead_times lt
                 WHERE lt.operating_rule_set_id = s.operating_rule_set_id) AS lead_times
          FROM operating_rule_sets s WHERE s.company_id = :c ORDER BY s.version_no
        """
    )
    before = (await db.execute(snapshot_sql, {"c": row.company_id})).all()
    lead_times = await _current_lead_times(db, row.company_id)
    evicted: list = []

    created = await _create(db, row, evicted=evicted)

    after = (await db.execute(snapshot_sql, {"c": row.company_id})).all()
    assert after[:-1] == before  # 기존 버전은 그대로
    new = after[-1]
    assert created.version_no == new.version_no == row.max_version_no + 1
    assert new.operating_rule_set_id == created.operating_rule_set_id
    assert (new.effective_from, new.safety_stock_days) == (date(2026, 7, 2), 21)
    assert new.lead_times == len(lead_times) > 0
    assert evicted == [row.company_id]  # 엔진 저장소 캐시에서 그 고객사를 버림

    details = await _service(db).list_rule_sets(
        actor=_actor(row.admin_user_id), company_id=row.company_id
    )
    newest = details[0]
    assert (newest.row.version_no, newest.is_current) == (created.version_no, True)
    assert newest.row.author_user_id == row.admin_user_id
    assert newest.row.author_role_code == "ADMIN"
    assert newest.row.created_at == NOW
    assert [d.is_current for d in details[1:]] == [False] * (len(details) - 1)


async def test_future_version_is_saved_but_not_current_yet(db_write_session):
    db = db_write_session
    row = await _assigned(db)

    created = await _create(db, row, effective_from=date(2026, 7, 3))

    details = await _service(db).list_rule_sets(
        actor=_actor(row.admin_user_id), company_id=row.company_id
    )
    assert (details[0].row.version_no, details[0].is_current) == (
        created.version_no,
        False,
    )
    assert details[1].is_current


async def test_create_does_not_touch_other_companies(db_write_session):
    db = db_write_session
    row = await _assigned(db)
    others_sql = text(
        "SELECT company_id, count(*) FROM operating_rule_sets"
        " WHERE company_id <> :c GROUP BY company_id ORDER BY company_id"
    )
    before = (await db.execute(others_sql, {"c": row.company_id})).all()

    await _create(db, row)

    assert (await db.execute(others_sql, {"c": row.company_id})).all() == before
    details = await _service(db).list_rule_sets(
        actor=_actor(row.admin_user_id), company_id=row.company_id
    )
    own = set(
        await db.scalars(
            text(
                "SELECT operating_rule_set_id FROM operating_rule_sets WHERE company_id = :c"
            ),
            {"c": row.company_id},
        )
    )
    assert {d.row.operating_rule_set_id for d in details} == own


async def test_unassigned_admin_is_403_for_list_and_create(db_write_session):
    db = db_write_session
    row = await _assigned(db)
    stranger = _actor(uuid.uuid7())  # 어느 고객사에도 배정되지 않은 ADMIN
    count_sql = text("SELECT count(*) FROM operating_rule_sets WHERE company_id = :c")
    before = await db.scalar(count_sql, {"c": row.company_id})

    with pytest.raises(ForbiddenException):
        await _service(db).list_rule_sets(actor=stranger, company_id=row.company_id)
    with pytest.raises(ForbiddenException):
        await _service(db).create_rule_set(
            actor=stranger,
            company_id=row.company_id,
            effective_from=date(2026, 7, 2),
            safety_stock_days=21,
            sales_basis_days=28,
            allow_partial_fulfillment=None,
            lead_times=[],
        )

    assert await db.scalar(count_sql, {"c": row.company_id}) == before


async def test_admin_is_403_for_company_not_assigned_to(db_write_session):
    """배정된 ADMIN 이라도 배정되지 않은 다른 고객사는 볼 수 없다(NFR-005)"""
    db = db_write_session
    row = await _assigned(db)
    other = await db.scalar(
        text(
            """
            SELECT c.company_id FROM companies c
             WHERE NOT EXISTS (SELECT 1 FROM admin_assignments a
                                WHERE a.company_id = c.company_id
                                  AND a.admin_user_id = :u)
             ORDER BY c.company_id LIMIT 1
            """
        ),
        {"u": row.admin_user_id},
    )
    if other is None:
        # 확정 DB 는 ADMIN 한 명이 모든 고객사에 배정돼 있다. 한 곳의 배정을 지워 만든다(롤백됨)
        other = await db.scalar(
            text(
                "DELETE FROM admin_assignments WHERE admin_assignment_id ="
                " (SELECT admin_assignment_id FROM admin_assignments"
                "   WHERE admin_user_id = :u AND company_id <> :c"
                "   ORDER BY company_id LIMIT 1) RETURNING company_id"
            ),
            {"u": row.admin_user_id, "c": row.company_id},
        )
    if other is None:
        pytest.skip("고객사가 하나뿐인 DB")

    with pytest.raises(ForbiddenException):
        await _service(db).list_rule_sets(
            actor=_actor(row.admin_user_id), company_id=other
        )


async def test_warehouse_of_other_company_is_invalid_lead_time(db_write_session):
    db = db_write_session
    row = await _assigned(db)
    foreign = await db.scalar(
        text(
            "SELECT warehouse_id FROM warehouses WHERE company_id <> :c"
            " ORDER BY warehouse_id LIMIT 1"
        ),
        {"c": row.company_id},
    )
    if foreign is None:
        pytest.skip("다른 고객사 창고가 없는 DB")

    with pytest.raises(InvalidLeadTimeException):
        await _create(
            db,
            row,
            lead_times=[
                NewLeadTime("OUTBOUND_HANDLING", foreign, None, 1, "CALENDAR_DAY")
            ],
        )


# --- 최종 선택과의 연결 (serviceflow 5.2 재확인 5, 인수 조건) -----------------------------


async def test_effective_new_version_blocks_final_decision_of_run_on_old_version(
    db_write_session,
):
    db = db_write_session
    run = await _ready_run(db)
    row = await _assigned(db, run.company_id)
    status_sql = text("SELECT status FROM analysis_runs WHERE analysis_run_id = :r")

    # 적용 시작일이 선택일(DECISION_NOW 의 날짜)인 새 버전
    await _create(db, row, effective_from=DECISION_NOW.date(), now=DECISION_NOW)

    # 저장만으로는 실행을 STALE 로 바꾸지 않는다
    assert await db.scalar(status_sql, {"r": run.analysis_run_id}) == "READY_FOR_REVIEW"
    with pytest.raises(DecisionException) as raised:
        await _decide(db, run)
    assert raised.value.error_code is DecisionErrorCode.OPERATING_RULE_CHANGED
    assert await db.scalar(status_sql, {"r": run.analysis_run_id}) == "READY_FOR_REVIEW"


async def test_future_version_does_not_block_final_decision(db_write_session):
    db = db_write_session
    run = await _ready_run(db)
    row = await _assigned(db, run.company_id)

    # 적용 시작일이 선택일 다음 날: 아직 적용 전이라 이전 버전으로 분석한 실행을 그대로 선택할 수 있다
    await _create(db, row, effective_from=date(2026, 7, 3), now=DECISION_NOW)

    result = await _decide(db, run)
    assert result.status == "FINALIZED"
