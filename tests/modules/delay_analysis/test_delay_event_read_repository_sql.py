from datetime import timedelta

import pytest
from sqlalchemy import text

from app.modules.delay_analysis.infrastructure.repository.delay_event_read_repository import (
    DelayEventFilter,
    DelayEventReadRepository,
)

"""
    지연 목록 감지 기간 SQL 스모크 테스트 (실제 DB, 읽기 전용)
      - 감지 기간은 [from, to) 입니다 (명세 v6.2 C18). to 와 같은 감지 시각은 빠지고, from 과 같은 감지 시각은 들어갑니다.
"""


async def test_detected_period_includes_from_and_excludes_to(db_session):
    db = db_session
    row = (
        await db.execute(
            text(
                "SELECT company_id, detected_at, count(*) AS n FROM delay_events"
                " GROUP BY company_id, detected_at ORDER BY detected_at LIMIT 1"
            )
        )
    ).one_or_none()
    if row is None:
        pytest.skip("지연 이벤트가 없는 DB")
    repo = DelayEventReadRepository(db)
    moment, tick = row.detected_at, timedelta(microseconds=1)

    async def count(**period):
        return await repo.count_delay_events(row.company_id, DelayEventFilter(**period))

    assert await count(detected_from=moment, detected_to=moment + tick) == row.n
    assert await count(detected_from=moment, detected_to=moment) == 0
    assert await count(detected_to=moment) == 0
    assert await count(detected_from=moment + tick, detected_to=moment + 2 * tick) == 0
