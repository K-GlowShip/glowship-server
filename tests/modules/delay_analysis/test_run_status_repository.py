import uuid
from dataclasses import dataclass, field

from sqlalchemy.dialects import postgresql

from app.modules.delay_analysis.application.agent.graph import build_analysis_graph
from app.modules.delay_analysis.application.agent.state import AnalysisOutcome
from app.modules.delay_analysis.infrastructure.repository.run_status_repository import (
    AnalysisRunStatusReader,
)

COMPANY_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()


# --- 가짜 세션 -----------------------------------------------------------------


@dataclass
class FakeSession:
    """실행한 SQL 을 기록하고, scalar 는 statuses 에서 실행 ID 로 상태를 찾아 돌려줍니다."""

    statuses: dict
    executed: list = field(default_factory=list)
    scalar_statements: list = field(default_factory=list)
    closed: bool = False

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))

    async def scalar(self, statement):
        self.scalar_statements.append(statement)
        (analysis_run_id,) = statement.compile(
            dialect=postgresql.dialect()
        ).params.values()
        return self.statuses.get(analysis_run_id)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True


@dataclass
class FakeSessionFactory:
    """호출될 때마다 새 세션을 만들고 기록합니다. statuses 는 세션끼리 공유합니다(= 커밋된 DB 상태)."""

    statuses: dict = field(default_factory=dict)
    sessions: list = field(default_factory=list)

    def __call__(self):
        session = FakeSession(self.statuses)
        self.sessions.append(session)
        return session


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


# --- 조회 -----------------------------------------------------------------------


async def test_get_status_sets_company_context_and_reads_status_column_only():
    factory = FakeSessionFactory({RUN_ID: "ANALYZING_IMPACT"})

    status = await AnalysisRunStatusReader(factory, COMPANY_ID).get_status(RUN_ID)

    assert status == "ANALYZING_IMPACT"
    [session] = factory.sessions
    [(sql, params)] = session.executed
    assert "set_config('app.company_id'" in sql
    assert params == {"user_id": "", "company_id": str(COMPANY_ID)}
    [statement] = session.scalar_statements
    assert _sql(statement).startswith(
        "SELECT analysis_runs.status \nFROM analysis_runs"
    )


async def test_get_status_returns_none_for_missing_or_hidden_run():
    factory = FakeSessionFactory()

    assert await AnalysisRunStatusReader(factory, COMPANY_ID).get_status(RUN_ID) is None


async def test_each_call_opens_and_closes_a_new_session():
    """그래프 실행 내내 트랜잭션을 열어 두지 않고, 커밋된 최신 상태를 매번 새로 읽는다"""
    factory = FakeSessionFactory({RUN_ID: "ANALYZING_IMPACT"})
    reader = AnalysisRunStatusReader(factory, COMPANY_ID)

    first = await reader.get_status(RUN_ID)
    factory.statuses[RUN_ID] = "STALE"  # 수집 적재가 다른 트랜잭션에서 STALE 을 커밋함
    second = await reader.get_status(RUN_ID)

    assert (first, second) == ("ANALYZING_IMPACT", "STALE")
    assert len(factory.sessions) == 2
    assert all(session.closed for session in factory.sessions)


# --- 그래프 연결 -----------------------------------------------------------------


async def test_graph_stops_when_reader_reports_stale():
    """그래프의 노드 경계 확인(is_run_stale)에 그대로 꽂혀 동작한다"""
    factory = FakeSessionFactory({RUN_ID: "STALE"})
    reader = AnalysisRunStatusReader(factory, COMPANY_ID)
    state = {
        "analysis_run_id": RUN_ID,
        "company_id": COMPANY_ID,
        "delay_event_id": uuid.uuid7(),
        "operating_rule_set_id": uuid.uuid7(),
    }

    result = await build_analysis_graph().ainvoke(
        state, {"configurable": {"run_status_reader": reader}}
    )

    assert result["outcome"] == AnalysisOutcome.STALE
    assert "validate" not in result["visited_nodes"]
