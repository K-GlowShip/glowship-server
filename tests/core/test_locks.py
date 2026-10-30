import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.core.locks import lock_analysis_run, lock_analysis_runs, lock_company

COMPANY_ID = uuid.uuid7()


# --- 가짜 세션 -----------------------------------------------------------------


@dataclass
class FakeSession:
    """실행한 SQL 을 기록하고, scalar 는 runs 에 있는 실행 ID 만 돌려줍니다."""

    runs: dict = field(default_factory=dict)
    executed: list = field(default_factory=list)
    locked_ids: list = field(default_factory=list)
    scalar_statements: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((str(statement), params))

    async def scalar(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        (analysis_run_id,) = compiled.params.values()
        self.scalar_statements.append(statement)
        self.locked_ids.append(analysis_run_id)
        return self.runs.get(analysis_run_id)


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _run(analysis_run_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(analysis_run_id=analysis_run_id)


# --- L1 고객사 잠금 -------------------------------------------------------------


async def test_lock_company_uses_transaction_advisory_lock_on_company_key():
    session = FakeSession()

    await lock_company(session, COMPANY_ID)

    [(sql, params)] = session.executed
    assert "pg_advisory_xact_lock" in sql
    assert "hashtextextended" in sql
    assert params == {"company_id": str(COMPANY_ID)}


# --- L2 실행 잠금 ---------------------------------------------------------------


async def test_lock_analysis_run_selects_run_for_update_and_refreshes_it():
    run_id = uuid.uuid7()
    run = _run(run_id)
    session = FakeSession(runs={run_id: run})

    result = await lock_analysis_run(session, run_id)

    assert result is run
    [statement] = session.scalar_statements
    assert "FOR UPDATE" in _sql(statement)
    assert statement.get_execution_options()["populate_existing"] is True


async def test_lock_analysis_run_returns_none_for_missing_run():
    session = FakeSession()

    assert await lock_analysis_run(session, uuid.uuid7()) is None


async def test_lock_analysis_runs_locks_in_id_order_without_duplicates():
    first, second, third = sorted(uuid.uuid4() for _ in range(3))
    runs = {run_id: _run(run_id) for run_id in (first, second, third)}
    session = FakeSession(runs=runs)

    result = await lock_analysis_runs(session, [third, first, second, first])

    assert session.locked_ids == [first, second, third]
    assert [run.analysis_run_id for run in result] == [first, second, third]


async def test_lock_analysis_runs_skips_missing_runs():
    existing, missing = sorted(uuid.uuid4() for _ in range(2))
    session = FakeSession(runs={existing: _run(existing)})

    result = await lock_analysis_runs(session, [missing, existing])

    assert session.locked_ids == [existing, missing]
    assert [run.analysis_run_id for run in result] == [existing]
