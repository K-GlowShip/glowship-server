import uuid
from datetime import UTC, datetime

import pytest

from app.core.security.tenant import TenantContext
from app.modules.decision.application.draft_policy import DraftTarget
from app.modules.decision.application.exception.decision_exceptions import (
    DecisionErrorCode,
    DecisionException,
)
from app.modules.decision.application.member_draft_service import MemberDraftService

"""
    의견·선택 초안 저장 서비스 (API 18): 실행 조회 → 허용 판정 → 대응안 소속 → 한 행 추가 순서와,
    거부한 시도는 행을 만들지 않는지(명세 4.6) 확인한다.
"""

TENANT = TenantContext(user_id=uuid.uuid7(), company_id=uuid.uuid7())
RUN_ID = uuid.uuid7()
OPTION_ID = uuid.uuid7()
NOW = datetime(2026, 7, 2, 1, 30, tzinfo=UTC)
READY = DraftTarget("READY_FOR_REVIEW", True, None, True)


class FakeRepository:
    def __init__(self, target: DraftTarget | None = READY, options=(OPTION_ID,)):
        self.target = target
        self.options = set(options)
        self.inserted: list = []
        self.option_checks: list = []

    async def find_draft_target(self, company_id, analysis_run_id):
        assert (company_id, analysis_run_id) == (TENANT.company_id, RUN_ID)
        return self.target

    async def option_in_run(self, company_id, analysis_run_id, response_option_id):
        self.option_checks.append(response_option_id)
        return (
            company_id == TENANT.company_id
            and analysis_run_id == RUN_ID
            and response_option_id in self.options
        )

    async def insert_member_draft(self, company_id, draft):
        self.inserted.append((company_id, draft))


async def _save(repo, option=OPTION_ID, opinion=None):
    return await MemberDraftService(repo, now=lambda: NOW).save_draft(
        TENANT,
        analysis_run_id=RUN_ID,
        response_option_id=option,
        opinion_text=opinion,
    )


async def test_saves_one_row_for_requesting_member():
    repo = FakeRepository()

    saved = await _save(repo, opinion="항공 전환 검토")

    [(company_id, draft)] = repo.inserted
    assert company_id == TENANT.company_id
    assert (
        draft.analysis_run_id,
        draft.user_id,
        draft.response_option_id,
        draft.opinion_text,
        draft.created_at,
    ) == (RUN_ID, TENANT.user_id, OPTION_ID, "항공 전환 검토", NOW)
    assert (saved.member_draft_id, saved.created_at) == (draft.member_draft_id, NOW)


async def test_opinion_only_skips_option_check():
    repo = FakeRepository()

    await _save(repo, option=None, opinion="의견만")

    assert repo.option_checks == []
    assert len(repo.inserted) == 1


async def test_unknown_run_is_404_and_saves_nothing():
    repo = FakeRepository(target=None)

    with pytest.raises(DecisionException) as raised:
        await _save(repo)

    assert raised.value.error_code == DecisionErrorCode.ANALYSIS_RUN_NOT_FOUND
    assert repo.inserted == []


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (
            DraftTarget("READY_FOR_REVIEW", True, None, False),
            DecisionErrorCode.EPISODE_CLOSED,
        ),
        (
            DraftTarget("VALIDATING_OUTPUT", True, None, True),
            DecisionErrorCode.EXPLANATION_NOT_READY,
        ),
        (DraftTarget("STALE", True, None, True), DecisionErrorCode.ANALYSIS_STALE),
        (
            DraftTarget("FINALIZED", True, None, True),
            DecisionErrorCode.FINAL_DECISION_EXISTS,
        ),
        (
            DraftTarget("READY_FOR_REVIEW", False, None, True),
            DecisionErrorCode.NOT_CURRENT_RUN,
        ),
        (DraftTarget("NO_RISK", True, None, True), DecisionErrorCode.RUN_NOT_READY),
    ],
)
async def test_policy_rejection_comes_before_option_check(target, expected):
    """판정에서 거부되면 대응안을 보지 않고 행도 만들지 않는다 (엔진 draft_check 가 먼저)"""
    repo = FakeRepository(target=target, options=())

    with pytest.raises(DecisionException) as raised:
        await _save(repo)

    assert raised.value.error_code == expected
    assert repo.option_checks == []
    assert repo.inserted == []


async def test_option_of_other_run_is_rejected():
    repo = FakeRepository(options=())

    with pytest.raises(DecisionException) as raised:
        await _save(repo)

    assert raised.value.error_code == DecisionErrorCode.OPTION_NOT_IN_RUN
    assert repo.inserted == []
