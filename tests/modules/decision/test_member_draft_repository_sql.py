import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.modules.decision.application.draft_policy import DraftTarget, draft_rejection
from app.modules.decision.infrastructure.repository.member_draft_repository import (
    MemberDraftRepository,
    NewMemberDraft,
)

"""
    의견·선택 초안 저장 리포지토리 SQL 테스트 (실제 DB, 쓰기는 끝나면 롤백)
      - 확정 DB 의 모든 실행으로 판정 입력(상태·현재 실행·실패 코드·에피소드 열림)과 대응안 소속을 확인하고,
        롤백되는 트랜잭션 안에서 초안을 추가해 DB 제약(ck1, 대응안 FK)을 통과하는지 본다.
      - 확정 DB 는 지연이 모두 닫혀 있어, 열린 에피소드는 롤백 트랜잭션에서 closed_at 을 지워 만든다.
      - DB 가 없으면 db_session·db_write_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")

_RUNS = text(
    """
    SELECT r.company_id, r.analysis_run_id, r.status, r.is_latest, r.failure_code,
           e.delay_event_id, e.closed_at
      FROM analysis_runs r
      JOIN delay_events e ON e.delay_event_id = r.delay_event_id
     ORDER BY r.analysis_run_id
    """
)

# 대응안이 있는 실행 하나와 그 고객사의 팀원
_RUN_WITH_OPTION = text(
    """
    SELECT o.company_id, o.analysis_run_id, o.response_option_id, r.delay_event_id,
           (SELECT u.user_id FROM users u
             WHERE u.company_id = o.company_id AND u.role_code = 'CLIENT_MEMBER'
             ORDER BY u.user_id LIMIT 1) AS member_id
      FROM response_options o
      JOIN analysis_runs r ON r.analysis_run_id = o.analysis_run_id
     ORDER BY o.response_option_id
     LIMIT 1
    """
)


async def test_draft_target_matches_every_run(db_session):
    repo = MemberDraftRepository(db_session)

    for run in (await db_session.execute(_RUNS)).all():
        target = await repo.find_draft_target(run.company_id, run.analysis_run_id)

        assert target == DraftTarget(
            status=run.status,
            is_latest=run.is_latest,
            failure_code=run.failure_code,
            episode_open=run.closed_at is None,
        )


async def test_draft_target_is_none_for_other_company_or_unknown_run(db_session):
    repo = MemberDraftRepository(db_session)
    run = (await db_session.execute(_RUNS)).first()
    if run is None:
        return  # 확정 데이터가 없는 DB

    assert await repo.find_draft_target(UNKNOWN_ID, run.analysis_run_id) is None
    assert await repo.find_draft_target(run.company_id, UNKNOWN_ID) is None


async def test_option_in_run_only_for_same_run_and_company(db_session):
    repo = MemberDraftRepository(db_session)
    options = (
        await db_session.execute(
            text(
                "SELECT company_id, analysis_run_id, response_option_id"
                " FROM response_options ORDER BY response_option_id"
            )
        )
    ).all()
    run_ids = {option.analysis_run_id for option in options}

    for option in options:
        assert await repo.option_in_run(
            option.company_id, option.analysis_run_id, option.response_option_id
        )
        assert not await repo.option_in_run(
            UNKNOWN_ID, option.analysis_run_id, option.response_option_id
        )
        for other_run_id in run_ids - {option.analysis_run_id}:
            assert not await repo.option_in_run(
                option.company_id, other_run_id, option.response_option_id
            )


async def _sample(db):
    sample = (await db.execute(_RUN_WITH_OPTION)).first()
    if sample is None or sample.member_id is None:
        pytest.skip("대응안이 있는 실행과 팀원이 없는 DB")
    return sample


def _draft(sample, **kwargs) -> NewMemberDraft:
    values = {
        "member_draft_id": uuid.uuid7(),
        "analysis_run_id": sample.analysis_run_id,
        "user_id": sample.member_id,
        "response_option_id": sample.response_option_id,
        "opinion_text": None,
        "created_at": datetime(2026, 7, 2, 1, 0, tzinfo=UTC),
    } | kwargs
    return NewMemberDraft(**values)


async def test_insert_member_draft_option_or_opinion(db_write_session):
    db = db_write_session
    sample = await _sample(db)
    repo = MemberDraftRepository(db)
    with_option = _draft(sample)
    with_opinion = _draft(
        sample, response_option_id=None, opinion_text="항공 전환 검토"
    )

    for draft in (with_option, with_opinion):
        await repo.insert_member_draft(sample.company_id, draft)

    stored = (
        await db.execute(
            text(
                "SELECT member_draft_id, company_id, analysis_run_id, user_id,"
                " response_option_id, opinion_text, created_at"
                " FROM member_drafts WHERE member_draft_id IN (:a, :b)"
                " ORDER BY opinion_text NULLS FIRST"
            ),
            {"a": with_option.member_draft_id, "b": with_opinion.member_draft_id},
        )
    ).all()
    assert [tuple(row) for row in stored] == [
        (
            draft.member_draft_id,
            sample.company_id,
            draft.analysis_run_id,
            draft.user_id,
            draft.response_option_id,
            draft.opinion_text,
            draft.created_at,
        )
        for draft in (with_option, with_opinion)
    ]


async def test_insert_rejects_empty_draft(db_write_session):
    """member_drafts_ck1: 대응안·의견 둘 다 없으면 DB 가 막는다 (API 는 그 전에 422)"""
    db = db_write_session
    sample = await _sample(db)

    with pytest.raises(IntegrityError, match="member_drafts_ck1"):
        await MemberDraftRepository(db).insert_member_draft(
            sample.company_id, _draft(sample, response_option_id=None)
        )


async def test_insert_rejects_option_of_other_run(db_write_session):
    """fk_member_drafts__response_option_id: 다른 실행의 대응안이면 DB 가 막는다 (API 는 그 전에 OPTION_NOT_IN_RUN)"""
    db = db_write_session
    sample = await _sample(db)
    other_run_id = await db.scalar(
        text(
            "SELECT analysis_run_id FROM analysis_runs"
            " WHERE company_id = :c AND analysis_run_id <> :r"
            " ORDER BY analysis_run_id LIMIT 1"
        ),
        {"c": sample.company_id, "r": sample.analysis_run_id},
    )
    if other_run_id is None:
        pytest.skip("같은 고객사에 다른 실행이 없는 DB")

    with pytest.raises(IntegrityError, match="fk_member_drafts__response_option_id"):
        await MemberDraftRepository(db).insert_member_draft(
            sample.company_id, _draft(sample, analysis_run_id=other_run_id)
        )


async def test_reopened_episode_is_read_as_open(db_write_session):
    """확정 DB 는 지연이 모두 닫혀 EPISODE_CLOSED 만 나온다. closed_at 을 지우면 열림으로 읽고 판정이 다음 단계로 간다."""
    db = db_write_session
    sample = await _sample(db)
    repo = MemberDraftRepository(db)
    closed = await repo.find_draft_target(sample.company_id, sample.analysis_run_id)

    await db.execute(
        text(
            "UPDATE delay_events SET closed_at = NULL, close_reason = NULL"
            " WHERE delay_event_id = :e"
        ),
        {"e": sample.delay_event_id},
    )
    reopened = await repo.find_draft_target(sample.company_id, sample.analysis_run_id)

    assert reopened == DraftTarget(
        status=closed.status,
        is_latest=closed.is_latest,
        failure_code=closed.failure_code,
        episode_open=True,
    )
    if not closed.episode_open:
        assert draft_rejection(closed).code == "EPISODE_CLOSED"
    assert draft_rejection(reopened) is None or (
        draft_rejection(reopened).code != "EPISODE_CLOSED"
    )
