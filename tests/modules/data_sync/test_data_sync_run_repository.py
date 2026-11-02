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
RUN_1 = uuid.uuid7()
RUN_2 = uuid.uuid7()
T1 = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
T1_END = datetime(2026, 11, 2, 3, 0, 30, tzinfo=UTC)
T2 = datetime(2026, 11, 2, 3, 10, tzinfo=UTC)


@dataclass
class FakeSession:
    """실행한 문장과 파라미터를 기록하고 미리 넣어둔 행을 돌려준다."""

    rows: list = field(default_factory=list)
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((statement, params))
        return iter(self.rows)


def _row(source_domain, **overrides):
    values = {
        "source_domain": source_domain,
        "data_sync_run_id": RUN_1,
        "connection_method": "API",
        "status": "SUCCEEDED",
        "started_at": T1,
        "finished_at": T1_END,
        "received_record_count": 100,
        "loaded_record_count": 100,
        "rejected_record_count": 0,
        "latest_completed_status": "SUCCEEDED",
        "last_synced_at": T1_END,
        "open_issue_count": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_원천별_세_가지_최신_실행과_이슈_수를_한_문장으로_조회한다():
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
    # 명세 4.3 의 마지막 실행 필드를 latest CTE 에서 모두 가져온다
    for column in (
        "data_sync_run_id",
        "connection_method",
        "received_record_count",
        "loaded_record_count",
        "rejected_record_count",
    ):
        assert column in sql
    # 미해결 이슈는 실행을 거쳐 원천별로 센다
    assert "FROM data_validation_issues i" in sql
    assert "i.status = 'OPEN'" in sql
    assert "GROUP BY r.source_domain" in sql
    assert "COALESCE(o.open_issue_count, 0)" in sql
    assert "company_id = " in sql and "= ANY(" in sql
    assert params == {
        "company_id": COMPANY_ID,
        "source_domains": ["ORDER", "INVENTORY"],
    }


async def test_행을_원천별_요약으로_바꾸고_이력_없는_원천은_뺀다():
    session = FakeSession(
        rows=[
            _row(
                "ORDER",
                data_sync_run_id=RUN_2,
                status="FAILED",
                started_at=T2,
                finished_at=None,
                received_record_count=None,
                loaded_record_count=None,
                rejected_record_count=None,
                latest_completed_status="FAILED",
                last_synced_at=T1_END,
                open_issue_count=6,
            ),
            _row(
                "SHIPMENT",
                status="RUNNING",
                finished_at=None,
                received_record_count=None,
                loaded_record_count=None,
                rejected_record_count=None,
                latest_completed_status=None,
                last_synced_at=None,
            ),
        ]
    )

    summaries = await DataSyncRunRepository(session).find_latest_by_domain(
        COMPANY_ID, ["ORDER", "INVENTORY", "SHIPMENT"]
    )

    assert summaries == {
        "ORDER": LatestRunSummary(
            data_sync_run_id=RUN_2,
            connection_method="API",
            status="FAILED",
            started_at=T2,
            finished_at=None,
            received_count=None,
            loaded_count=None,
            rejected_count=None,
            latest_completed_status="FAILED",
            last_synced_at=T1_END,
            open_issue_count=6,
        ),
        "SHIPMENT": LatestRunSummary(
            data_sync_run_id=RUN_1,
            connection_method="API",
            status="RUNNING",
            started_at=T1,
            finished_at=None,
            received_count=None,
            loaded_count=None,
            rejected_count=None,
            latest_completed_status=None,
            last_synced_at=None,
            open_issue_count=0,
        ),
    }
    assert "INVENTORY" not in summaries
