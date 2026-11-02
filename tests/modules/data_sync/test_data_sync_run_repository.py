import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.modules.data_sync.infrastructure.repository.data_sync_run_repository import (
    DataSyncRunRepository,
    LatestRunSummary,
)

COMPANY_ID = uuid.uuid7()
T1 = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
T2 = datetime(2026, 11, 2, 3, 10, tzinfo=UTC)


@dataclass
class FakeSession:
    """실행한 문장과 파라미터를 기록하고 미리 넣어둔 행을 돌려준다."""

    rows: list = field(default_factory=list)
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((statement, params))
        return iter(self.rows)


def _row(source_domain, latest_started_at, latest_completed_status, last_synced_at):
    return SimpleNamespace(
        source_domain=source_domain,
        latest_started_at=latest_started_at,
        latest_completed_status=latest_completed_status,
        last_synced_at=last_synced_at,
    )


async def test_도메인별_세_가지_최신_실행을_한_문장으로_조회한다():
    session = FakeSession()

    await DataSyncRunRepository(session).find_latest_by_domain(
        COMPANY_ID, ["ORDER", "INVENTORY"]
    )

    [(statement, params)] = session.executed
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert sql.count("DISTINCT ON (source_domain)") == 3
    assert "status <> 'RUNNING'" in sql
    assert "status IN ('SUCCEEDED', 'PARTIAL_SUCCESS')" in sql
    assert "ORDER BY source_domain, applied_seq DESC" in sql
    assert "company_id = " in sql and "= ANY(" in sql
    assert params == {
        "company_id": COMPANY_ID,
        "source_domains": ["ORDER", "INVENTORY"],
    }


async def test_행을_도메인별_요약으로_바꾸고_이력_없는_도메인은_뺀다():
    session = FakeSession(
        rows=[
            _row("ORDER", T2, "FAILED", T1),
            _row("SHIPMENT", T1, None, None),
        ]
    )

    summaries = await DataSyncRunRepository(session).find_latest_by_domain(
        COMPANY_ID, ["ORDER", "INVENTORY", "SHIPMENT"]
    )

    assert summaries == {
        "ORDER": LatestRunSummary(
            latest_started_at=T2, latest_completed_status="FAILED", last_synced_at=T1
        ),
        "SHIPMENT": LatestRunSummary(
            latest_started_at=T1, latest_completed_status=None, last_synced_at=None
        ),
    }
    assert "INVENTORY" not in summaries
