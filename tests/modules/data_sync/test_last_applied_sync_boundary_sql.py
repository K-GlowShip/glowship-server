import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
)
from app.modules.data_sync.infrastructure.repository.ingest_repository import (
    IngestRepository,
)
from app.modules.delay_analysis.infrastructure.repository.run_creation_repository import (
    RunCreationRepository,
)

"""
    "고객사의 마지막 적재 수집 시작" 세 경로가 같은 값을 내는가 (이슈 #101, PR #85 리뷰 후속, #58 할 일 3) — 실제 DB, 끝나면 롤백
      - 세 메서드는 모두 data_sync 의 조회 함수 last_applied_sync_started_at 하나를 부른다(PR #85).
        IngestRepository.last_applied_started_at(수집 시작 확정, 이슈 #39 규칙 5)
        RunCreationRepository.find_last_applied_sync_started_at(분석 실행 시각 확인, 이슈 #50)
        DataSyncRunRepository.find_last_applied_started_at_of_company(판단 시각, DecisionClock)
      - 확정 데이터에는 소수 초·적용 안 된 수집이 없어 그대로 비교하면 아래 실수를 못 잡는다. 그래서 경계 값을 넣는다:
        적용 수집 시작이 …:00.789 면 …:00(초 아래 버림), 더 늦은 FAILED(적용 순번 없음)와 다른 고객사의 더 늦은 수집은 무시.
        (rouiv 님 확인: 조회에서 date_trunc 나 applied_seq IS NOT NULL 을 빼면 이 비교가 실패)
"""

APPLIED_AT = datetime(2026, 7, 2, 0, 0, 0, 789000, tzinfo=UTC)
EXPECTED = datetime(2026, 7, 2, 0, 0, 0, tzinfo=UTC)


async def _company(db, code):
    """확정 데이터의 고객사. 없으면(CI 의 빈 DB 등) 건너뛴다 — 다른 확정 DB 시험(test_manual_sync_sql._a_beauty)과 같음"""
    company_id = await db.scalar(
        text("SELECT company_id FROM companies WHERE company_code = :c"), {"c": code}
    )
    if company_id is None:
        pytest.skip(f"확정 데이터({code})가 없는 DB")
    return company_id


async def _insert_run(db, company_id, started_at, *, applied: bool):
    next_seq = await db.scalar(
        text(
            "SELECT coalesce(max(applied_seq), 0) + 1 FROM data_sync_runs WHERE company_id = :c"
        ),
        {"c": company_id},
    )
    await db.execute(
        text(
            """
            INSERT INTO data_sync_runs (data_sync_run_id, company_id, source_domain, connection_method,
                   started_at, finished_at, status, received_record_count, loaded_record_count,
                   rejected_record_count, applied_seq)
            VALUES (:r, :c, 'ORDER', 'API', :s, :f, :st, :rc, :lc, :jc, :seq)
            """
        ),
        {
            "r": uuid.uuid7(),
            "c": company_id,
            "s": started_at,
            "f": started_at.replace(microsecond=0).replace(second=30),
            "st": "SUCCEEDED" if applied else "FAILED",
            "rc": 0 if applied else None,
            "lc": 0 if applied else None,
            "jc": 0 if applied else None,
            "seq": next_seq if applied else None,
        },
    )


async def _three(db, company_id):
    return (
        await IngestRepository(db).last_applied_started_at(company_id),
        await RunCreationRepository(db).find_last_applied_sync_started_at(company_id),
        await DataSyncRunRepository(db).find_last_applied_started_at_of_company(
            company_id
        ),
    )


async def test_three_paths_give_same_whole_second_and_ignore_failed_and_other_company(
    db_write_session,
):
    db = db_write_session
    a = await _company(db, "A_BEAUTY")
    b = await _company(db, "B_COSMETICS")
    await _insert_run(db, a, APPLIED_AT, applied=True)  # 적용됨, 소수 초
    await _insert_run(  # 더 늦지만 FAILED(적용 순번 없음) → 무시
        db, a, datetime(2026, 7, 2, 0, 5, tzinfo=UTC), applied=False
    )
    await _insert_run(  # 다른 고객사의 더 늦은 적용 → 무시
        db, b, datetime(2026, 7, 3, 0, 0, tzinfo=UTC), applied=True
    )

    assert await _three(db, a) == (EXPECTED, EXPECTED, EXPECTED)


async def test_three_paths_agree_on_confirmed_data(db_write_session):
    """확정 데이터(마지막 수집 07-01 00:04)에서도 세 경로가 같은 값"""
    db = db_write_session
    a = await _company(db, "A_BEAUTY")
    first, *rest = await _three(db, a)
    if first != datetime(2026, 7, 1, 0, 4, tzinfo=UTC):
        pytest.skip(f"확정 데이터(마지막 수집 07-01 00:04)가 아닌 DB: {first}")

    assert rest == [first, first]
