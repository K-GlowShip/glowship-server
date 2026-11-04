import logging
import uuid
from dataclasses import dataclass, field

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent.graph import build_analysis_graph
from app.modules.delay_analysis.application.agent.state import AnalysisOutcome
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun
from app.modules.delay_analysis.infrastructure.repository.run_status_repository import (
    AnalysisRunStatusReader,
)

COMPANY_ID = uuid.uuid7()
RUN_ID = uuid.uuid7()


# --- 가짜 세션 -----------------------------------------------------------------


@dataclass
class FakeSession:
    """실행한 SQL 을 기록하고, scalar 는 조건의 실행 ID·고객사로 statuses 에서 상태를 찾아 돌려줍니다.
    statuses 의 실행은 모두 COMPANY_ID 고객사 것으로 봅니다(DB 의 analysis_runs_uk1 과 같은 조건)."""

    statuses: dict
    executed: list = field(default_factory=list)
    scalar_statements: list = field(default_factory=list)
    closed: bool = False

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))

    async def scalar(self, statement):
        self.scalar_statements.append(statement)
        params = statement.compile(dialect=postgresql.dialect()).params
        run_ids = [v for k, v in params.items() if k.startswith("analysis_run_id")]
        company_ids = [v for k, v in params.items() if k.startswith("company_id")]
        if company_ids != [
            COMPANY_ID
        ]:  # 조건에 고객사가 없거나 다른 고객사면 보이지 않는다
            return None
        return self.statuses.get(run_ids[0])

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


async def test_get_status_filters_by_company_and_run_id():
    """RLS 에만 기대지 않고 조회 조건에 고객사를 넣는다 (RLS 가 없는 접속에서도 다른 고객사 실행은 안 보임)"""
    factory = FakeSessionFactory({RUN_ID: "ANALYZING_IMPACT"})

    await AnalysisRunStatusReader(factory, COMPANY_ID).get_status(RUN_ID)

    [statement] = factory.sessions[0].scalar_statements
    where = _sql(statement).split("WHERE", 1)[1]
    assert "analysis_runs.company_id = " in where
    assert "analysis_runs.analysis_run_id = " in where
    assert set(statement.compile(dialect=postgresql.dialect()).params.values()) == {
        COMPANY_ID,
        RUN_ID,
    }


async def test_other_company_reader_does_not_see_the_run():
    factory = FakeSessionFactory({RUN_ID: "STALE"})

    assert (
        await AnalysisRunStatusReader(factory, uuid.uuid7()).get_status(RUN_ID) is None
    )


async def test_get_status_returns_none_for_missing_or_hidden_run_with_warning(caplog):
    factory = FakeSessionFactory()

    with caplog.at_level(logging.WARNING):
        status = await AnalysisRunStatusReader(factory, COMPANY_ID).get_status(RUN_ID)

    assert status is None
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert str(RUN_ID) in record.getMessage()
    assert str(COMPANY_ID) in record.getMessage()


async def test_found_status_logs_nothing(caplog):
    factory = FakeSessionFactory({RUN_ID: "ANALYZING_IMPACT"})

    with caplog.at_level(logging.WARNING):
        await AnalysisRunStatusReader(factory, COMPANY_ID).get_status(RUN_ID)

    assert caplog.records == []


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


# --- 실제 DB (RLS 를 우회하는 로컬 superuser 접속) ---------------------------------


async def test_real_db_other_company_id_does_not_read_run_even_without_rls():
    """로컬은 superuser 라 RLS 가 적용되지 않는다. 조회 조건의 company_id 만으로 다른 고객사 실행이 막혀야 한다"""
    engine = create_async_engine(
        get_settings().database_url, poolclass=NullPool, connect_args={"timeout": 2}
    )
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                row = (
                    await db.execute(
                        select(
                            AnalysisRun.company_id,
                            AnalysisRun.analysis_run_id,
                            AnalysisRun.status,
                        ).limit(1)
                    )
                ).one_or_none()
                other = None
                if row is not None:
                    other = await db.scalar(
                        text(
                            "SELECT company_id FROM companies WHERE company_id <> :c LIMIT 1"
                        ),
                        {"c": row[0]},
                    )
        except (OSError, SQLAlchemyError) as error:
            pytest.skip(f"DB 에 접속할 수 없어 건너뜀: {type(error).__name__}")
        if row is None or other is None:
            pytest.skip("분석 실행 또는 다른 고객사가 없는 DB 라 건너뜀")
        company_id, run_id, status = row

        assert (
            await AnalysisRunStatusReader(factory, company_id).get_status(run_id)
            == status
        )
        assert await AnalysisRunStatusReader(factory, other).get_status(run_id) is None
    finally:
        await engine.dispose()
