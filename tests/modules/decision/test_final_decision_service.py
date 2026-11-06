import uuid
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from app.core.security.tenant import TenantContext
from app.modules.decision.application import final_decision_service
from app.modules.decision.application.decision_clock import DecisionClock
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
    DecisionException,
)
from app.modules.decision.application.final_decision_service import (
    FinalDecisionService,
)
from app.modules.decision.application.selection_policy import (
    LeadTimes,
    SelectionOption,
    SelectionTarget,
)
from app.modules.decision.infrastructure.repository.final_decision_repository import (
    StoredFinalDecision,
)

"""
    최종 선택 서비스 (API 19) 대역 시험: 시간 경과(TIME_ELAPSED)면 실행을 STALE 로 기록해 먼저 커밋한 뒤 거부하고,
    final_decisions 는 만들지 않는다 (명세 4.6 결과 1). 확정 DB 에는 회복 주문이 없어 SQL 로는 이 경로를 만들 수 없다.
"""

TENANT = TenantContext(user_id=uuid.uuid7(), company_id=uuid.uuid7())
RUN_ID = uuid.uuid7()
OPTION_ID = uuid.uuid7()
NOW = datetime(2026, 7, 10, 0, 3, tzinfo=UTC)
LT = LeadTimes(
    oh_kr=1, air_kr=3, inbound_dest=2, oh_donor=1, cd_cross=4, oh_dest=1, cd_same=2
)
TARGET = SelectionTarget(
    status="READY_FOR_REVIEW",
    is_latest=True,
    failure_code=None,
    episode_open=True,
    operating_rule_set_id="rule",
    active_operating_rule_set_id="rule",
    # 다른 권역: 선택일 07-10 + 1 + 4 > 약속일 07-12 → 시간 경과
    option=SelectionOption("OTHER_REGION_FULFILL", "FEASIBLE", (date(2026, 7, 12),)),
    lead_times=LT,
    latest_expected_3pl_inbound_date=date(2026, 7, 20),
    planned_kr_release_date=date(2026, 7, 5),
    selection_day=NOW.date(),
)


class FakeRepository:
    def __init__(self, target, stored=None):
        self.target = target
        self.stored = stored
        self.inserted = []

    async def find_by_idempotency_key(self, company_id, key):
        return self.stored

    async def find_selection_target(self, company_id, run_id, option_id, day):
        assert day == NOW.date()
        return self.target

    async def insert_final_decision(self, company_id, decision):
        self.inserted.append(decision)


class FakeSession:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass


@pytest.fixture
def run(monkeypatch):
    locked = SimpleNamespace(status="READY_FOR_REVIEW")

    async def fake_lock(db, run_id):
        assert run_id == RUN_ID
        return locked

    monkeypatch.setattr(final_decision_service, "lock_analysis_run", fake_lock)
    return locked


async def _decide(repo, db):
    return await FinalDecisionService(db, repo, clock=DecisionClock.fixed(NOW)).decide(
        TENANT,
        analysis_run_id=RUN_ID,
        response_option_id=OPTION_ID,
        reason_text="이유",
        idempotency_key="key-1",
    )


async def test_time_elapsed_records_stale_commits_then_rejects(run):
    repo, db = FakeRepository(TARGET), FakeSession()

    with pytest.raises(DecisionException) as raised:
        await _decide(repo, db)

    assert raised.value.error_code == DecisionErrorCode.ANALYSIS_STALE_TIME_ELAPSED
    assert raised.value.error_code.code == "ANALYSIS_STALE"
    assert (run.status, run.stale_primary_reason, run.stale_reason_codes) == (
        "STALE",
        "TIME_ELAPSED",
        ["TIME_ELAPSED"],
    )
    assert (run.stale_detected_seq, run.stale_detected_at) == (None, NOW)
    assert db.commits == 1
    assert repo.inserted == []


async def test_other_rejection_does_not_commit(run):
    repo = FakeRepository(replace(TARGET, episode_open=False))
    db = FakeSession()

    with pytest.raises(DecisionException) as raised:
        await _decide(repo, db)

    assert raised.value.error_code == DecisionErrorCode.EPISODE_CLOSED
    assert (run.status, db.commits) == ("READY_FOR_REVIEW", 0)


async def test_same_key_same_run_returns_saved_without_locking(monkeypatch):
    async def must_not_lock(db, run_id):
        raise AssertionError("재요청은 잠그지 않는다")

    monkeypatch.setattr(final_decision_service, "lock_analysis_run", must_not_lock)
    stored = StoredFinalDecision(uuid.uuid7(), RUN_ID, NOW, 7)

    result = await _decide(FakeRepository(TARGET, stored), FakeSession())

    assert (result.final_decision_id, result.checked_seq, result.replayed) == (
        stored.final_decision_id,
        7,
        True,
    )


async def test_same_key_other_run_is_reused(run):
    stored = StoredFinalDecision(uuid.uuid7(), uuid.uuid7(), NOW, 7)

    with pytest.raises(DecisionException) as raised:
        await _decide(FakeRepository(TARGET, stored), FakeSession())

    assert raised.value.error_code == DecisionErrorCode.IDEMPOTENCY_KEY_REUSED
