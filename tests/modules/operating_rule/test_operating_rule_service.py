import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.core.exceptions import ForbiddenException
from app.core.security.auth import CurrentUser
from app.modules.company.application.exception.company_exceptions import (
    CompanyInactiveException,
    CompanyNotFoundException,
)
from app.modules.decision.application.decision_clock import DecisionClock
from app.modules.operating_rule.application.exception.operating_rule_exceptions import (
    InvalidLeadTimeException,
)
from app.modules.operating_rule.application.operating_rule_service import (
    OperatingRuleService,
    validate_lead_times,
)
from app.modules.operating_rule.infrastructure.repository.operating_rule_repository import (
    LeadTimeRow,
    NewLeadTime,
    NewRuleSet,
    RuleSetRow,
)

"""
    운영 기준 서비스 단위 테스트 (API 4·5, GLOW-153). 가짜 리포지토리로 DB 없이 실행
      - 권한 확인 순서(RLS 컨텍스트 → 배정 → 고객사), 리드타임 규칙, 버전 번호, 현재 버전 판정, 저장 뒤 커밋·캐시 버리기 순서
"""

ADMIN = CurrentUser(user_id=uuid.uuid7(), role_code="ADMIN", company_id=None)
COMPANY_ID = uuid.uuid7()
WAREHOUSE_A, WAREHOUSE_B = uuid.uuid7(), uuid.uuid7()
NOW = datetime(2026, 7, 2, 0, 3, tzinfo=UTC)
ACTIVE_COMPANY = SimpleNamespace(status="ACTIVE")
# 계정 관리 권한이 없는 배정. 운영 기준은 배정만 있으면 된다(명세 6절)
ASSIGNMENT = SimpleNamespace(can_manage_client_accounts=False)


def _row(version_no: int, effective_from: date) -> RuleSetRow:
    return RuleSetRow(
        operating_rule_set_id=uuid.UUID(int=version_no),
        version_no=version_no,
        effective_from=effective_from,
        safety_stock_days=14,
        sales_basis_days=28,
        allow_partial_fulfillment=False,
        created_at=NOW,
        author_user_id=ADMIN.user_id,
        author_display_name="운영 담당",
        author_role_code="ADMIN",
    )


def _lead_time(
    lead_time_type="OUTBOUND_HANDLING", warehouse_id=WAREHOUSE_A, to_region_code=None
) -> NewLeadTime:
    return NewLeadTime(lead_time_type, warehouse_id, to_region_code, 1, "CALENDAR_DAY")


@dataclass
class Log:
    """호출 순서 기록 (리포지토리·잠금·커밋·캐시가 함께 쓴다)"""

    events: list = field(default_factory=list)


@dataclass
class FakeDb:
    log: Log

    async def commit(self):
        self.log.events.append("commit")


@dataclass
class FakeCompanyRepository:
    log: Log
    company: object | None

    async def find_by_id(self, company_id):
        self.log.events.append("company")
        return self.company


@dataclass
class FakeAdminAssignmentRepository:
    log: Log
    assignment: object | None

    async def find_by_admin_and_company(self, admin_user_id, company_id):
        self.log.events.append(("assignment", admin_user_id, company_id))
        return self.assignment


@dataclass
class FakeOperatingRuleRepository:
    log: Log
    rows: list[RuleSetRow] = field(default_factory=list)
    lead_times: list[LeadTimeRow] = field(default_factory=list)
    max_version_no: int = 0
    inserted: list[tuple[uuid.UUID, NewRuleSet]] = field(default_factory=list)

    async def find_rule_sets(self, company_id):
        return self.rows

    async def find_lead_times(self, company_id):
        return self.lead_times

    async def find_max_version_no(self, company_id):
        self.log.events.append("max_version")
        return self.max_version_no

    async def find_warehouse_ids(self, company_id):
        return {WAREHOUSE_A, WAREHOUSE_B}

    async def insert_rule_set(self, company_id, new):
        self.log.events.append("insert")
        self.inserted.append((company_id, new))


@pytest.fixture
def log(monkeypatch) -> Log:
    """DB 없이 실행하므로 set_rls_context·lock_company 를 기록용 가짜로 바꾼다."""
    log = Log()
    module = "app.modules.operating_rule.application.operating_rule_service"

    async def _fake_set_rls_context(db, *, user_id, company_id):
        log.events.append(("ctx", user_id, company_id))

    async def _fake_lock_company(db, company_id):
        log.events.append(("lock", company_id))

    monkeypatch.setattr(f"{module}.set_rls_context", _fake_set_rls_context)
    monkeypatch.setattr(f"{module}.lock_company", _fake_lock_company)
    return log


def _service(
    log: Log, *, company=ACTIVE_COMPANY, assignment=ASSIGNMENT, **repo_fields
) -> tuple[OperatingRuleService, FakeOperatingRuleRepository]:
    repo = FakeOperatingRuleRepository(log, **repo_fields)
    service = OperatingRuleService(
        db=FakeDb(log),
        repo=repo,
        company_repo=FakeCompanyRepository(log, company),
        admin_assignment_repo=FakeAdminAssignmentRepository(log, assignment),
        clock=DecisionClock.fixed(NOW),
        on_saved=lambda company_id: log.events.append(("evict", company_id)),
    )
    return service, repo


async def _create(service: OperatingRuleService, lead_times=None):
    return await service.create_rule_set(
        actor=ADMIN,
        company_id=COMPANY_ID,
        effective_from=date(2026, 7, 3),
        safety_stock_days=10,
        sales_basis_days=28,
        allow_partial_fulfillment=None,
        lead_times=[_lead_time()] if lead_times is None else lead_times,
    )


# --- 권한 (NFR-005) ---------------------------------------------------------------


async def test_checks_assignment_before_company_after_setting_rls_context(log):
    service, _ = _service(log)

    await service.list_rule_sets(actor=ADMIN, company_id=COMPANY_ID)

    assert log.events[:3] == [
        ("ctx", ADMIN.user_id, COMPANY_ID),
        ("assignment", ADMIN.user_id, COMPANY_ID),
        "company",
    ]


@pytest.mark.parametrize("call", ["list", "create"])
async def test_unassigned_admin_is_403_without_reading_company(log, call):
    """배정이 없으면 고객사가 있는지조차 읽지 않는다(없는 고객사와 구분되지 않음)"""
    service, repo = _service(log, assignment=None)

    with pytest.raises(ForbiddenException):
        if call == "list":
            await service.list_rule_sets(actor=ADMIN, company_id=COMPANY_ID)
        else:
            await _create(service)

    assert "company" not in log.events
    assert repo.inserted == []


async def test_missing_company_is_404_and_inactive_company_is_409(log):
    with pytest.raises(CompanyNotFoundException):
        await _create(_service(log, company=None)[0])
    with pytest.raises(CompanyInactiveException):
        await _create(_service(log, company=SimpleNamespace(status="INACTIVE"))[0])


# --- 조회 (API 4) -----------------------------------------------------------------


async def test_current_version_is_highest_version_already_effective(log):
    # v3 은 적용 시작일이 내일이라 아직 아님, v2 가 현재 (최종 선택 재확인의 활성 버전과 같은 규칙)
    rows = [
        _row(3, date(2026, 7, 3)),
        _row(2, date(2026, 7, 2)),
        _row(1, date(2025, 1, 1)),
    ]
    service, _ = _service(log, rows=rows)

    details = await service.list_rule_sets(actor=ADMIN, company_id=COMPANY_ID)

    assert [(d.row.version_no, d.is_current) for d in details] == [
        (3, False),
        (2, True),
        (1, False),
    ]


async def test_no_version_is_current_when_none_is_effective_yet(log):
    service, _ = _service(log, rows=[_row(1, date(2026, 7, 3))])

    details = await service.list_rule_sets(actor=ADMIN, company_id=COMPANY_ID)

    assert [d.is_current for d in details] == [False]


async def test_lead_times_are_grouped_by_version(log):
    def lead_time(version_no: int, days: int) -> LeadTimeRow:
        return LeadTimeRow(
            operating_rule_set_id=uuid.UUID(int=version_no),
            lead_time_type="OUTBOUND_HANDLING",
            warehouse_id=WAREHOUSE_A,
            warehouse_code="WH-A",
            warehouse_region_code="WEST",
            to_region_code=None,
            days=days,
            day_basis="CALENDAR_DAY",
        )

    service, _ = _service(
        log,
        rows=[_row(2, date(2026, 7, 1)), _row(1, date(2025, 1, 1))],
        lead_times=[lead_time(1, 1), lead_time(2, 2), lead_time(2, 3)],
    )

    details = await service.list_rule_sets(actor=ADMIN, company_id=COMPANY_ID)

    assert [[lt.days for lt in d.lead_times] for d in details] == [[2, 3], [1]]


# --- 저장 (API 5) -----------------------------------------------------------------


async def test_create_makes_next_version_inside_company_lock(log):
    service, repo = _service(log, max_version_no=3)

    created = await _create(service)

    company_id, new = repo.inserted[0]
    assert company_id == COMPANY_ID
    assert created.version_no == new.version_no == 4
    assert created.operating_rule_set_id == new.operating_rule_set_id
    assert new.user_id == ADMIN.user_id  # 작성자 = 요청한 ADMIN
    assert new.created_at == NOW
    assert (new.effective_from, new.safety_stock_days) == (date(2026, 7, 3), 10)
    assert new.allow_partial_fulfillment is None
    assert new.lead_times == (_lead_time(),)
    # 잠근 뒤에 최대 버전을 읽고, 커밋한 뒤에 캐시를 버린다
    assert log.events[-5:] == [
        ("lock", COMPANY_ID),
        "max_version",
        "insert",
        "commit",
        ("evict", COMPANY_ID),
    ]


async def test_first_version_is_1(log):
    service, _ = _service(log, max_version_no=0)

    assert (await _create(service)).version_no == 1


@pytest.mark.parametrize(
    "lead_times",
    [
        [_lead_time("AIR_TRANSIT")],  # 도착 권역이 필수인 유형인데 없음
        [_lead_time("CUSTOMER_DELIVERY")],
        [_lead_time("OUTBOUND_HANDLING", to_region_code="WEST")],  # 넣을 수 없는 유형
        [_lead_time("INBOUND_HANDLING", to_region_code="EAST")],
        [_lead_time(), _lead_time()],  # 같은 키 중복
        [
            _lead_time("AIR_TRANSIT", to_region_code="WEST"),
            _lead_time("AIR_TRANSIT", to_region_code="WEST"),
        ],
        [_lead_time(warehouse_id=uuid.uuid7())],  # 이 고객사의 창고가 아님
    ],
    ids=[
        "air-no-region",
        "delivery-no-region",
        "outbound-with-region",
        "inbound-with-region",
        "duplicate",
        "duplicate-with-region",
        "foreign-warehouse",
    ],
)
async def test_invalid_lead_time_is_rejected_without_saving(log, lead_times):
    service, repo = _service(log)

    with pytest.raises(InvalidLeadTimeException):
        await _create(service, lead_times)

    assert repo.inserted == []
    assert "commit" not in log.events


def test_same_type_and_warehouse_with_different_regions_is_valid():
    validate_lead_times(
        [
            _lead_time("CUSTOMER_DELIVERY", WAREHOUSE_A, "WEST"),
            _lead_time("CUSTOMER_DELIVERY", WAREHOUSE_A, "EAST"),
            _lead_time("CUSTOMER_DELIVERY", WAREHOUSE_B, "WEST"),
            _lead_time("OUTBOUND_HANDLING", WAREHOUSE_A),
            _lead_time("INBOUND_HANDLING", WAREHOUSE_A),
        ],
        {WAREHOUSE_A, WAREHOUSE_B},
    )


async def test_empty_lead_times_is_saved(log):
    """명세는 리드타임 구성을 강제하지 않는다. 빠진 값은 분석이 확인 필요(MISSING_OPERATING_RULE)·보류로 알린다"""
    service, repo = _service(log)

    await _create(service, [])

    assert repo.inserted[0][1].lead_times == ()
