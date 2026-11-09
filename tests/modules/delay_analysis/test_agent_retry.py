import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, bindparam, text
from sqlalchemy.dialects import postgresql

from app.modules.delay_analysis.application.agent.retry import (
    EXPLANATION_GENERATION_FAILED,
    MAX_ATTEMPTS,
    PERMISSION_DENIED,
    REQUIRED_DATA_INVALID,
    TOOL_TEMPORARY_ERROR_EXHAUSTED,
    TOOL_TIMEOUT_EXHAUSTED,
    UNEXPECTED_TOOL_ERROR,
    AttemptRecord,
    FailureSummary,
    PermissionDeniedError,
    RequiredDataError,
    ToolFailure,
    TransientToolError,
    call_with_retry,
    explanation_failure,
    hold_summary,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from app.modules.delay_analysis.infrastructure.analysis_run import AnalysisRun

"""
    도구 재시도와 실패 요약 (docs/agent-flow/agent-retry.md)
      - 재시도: 최초 1 + 재시도 2, 오류별 재시도 여부, 시도 기록
      - 실패 요약: 만든 값이 analysis_runs CHECK(ck10~ck14·ck26) 원문을 실제 DB 에서 통과하는지(읽기 전용)
"""

STEP = AnalysisStep.ANALYZING_IMPACT


class FakeTool:
    """정해 둔 순서대로 오류를 던지거나 값을 돌려주는 가짜 도구"""

    def __init__(self, *script: Any):
        self.script = list(script)
        self.calls = 0

    async def __call__(self) -> Any:
        self.calls += 1
        item = self.script[min(self.calls, len(self.script)) - 1]
        if isinstance(item, BaseException):
            raise item
        return item


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


async def _fail(tool: FakeTool, **kwargs: Any) -> tuple[FailureSummary, Sleeps]:
    sleeps = Sleeps()
    with pytest.raises(ToolFailure) as caught:
        await call_with_retry("get_sales_history", STEP, tool, sleep=sleeps, **kwargs)
    return caught.value.summary, sleeps


# --- 재시도 ----------------------------------------------------------------------------


async def test_timeout_three_times_is_exhausted_failure():
    tool = FakeTool(TimeoutError(), TimeoutError(), TimeoutError())

    summary, sleeps = await _fail(tool)

    assert tool.calls == MAX_ATTEMPTS == 3
    assert sleeps.waits == [0.2, 0.5]
    # 정답 overlay E3 v2 = 엔진 bundle_verify rt1 과 같은 5개
    assert (
        summary.status,
        summary.failed_step,
        summary.failure_code,
        summary.attempt_count,
        summary.retry_exhausted,
    ) == (AnalysisOutcome.FAILED, STEP, TOOL_TIMEOUT_EXHAUSTED, 3, True)
    assert "get_sales_history" in summary.failure_message


async def test_success_after_two_transient_errors_returns_result():
    tool = FakeTool(TransientToolError(), TransientToolError(), {"rows": 3})
    records: list[AttemptRecord] = []

    result = await call_with_retry(
        "get_sales_history", STEP, tool, sleep=Sleeps(), on_attempt=records.append
    )

    assert result == {"rows": 3}
    assert tool.calls == 3
    assert [(r.attempt, r.error_code, r.will_retry) for r in records] == [
        (1, TOOL_TEMPORARY_ERROR_EXHAUSTED, True),
        (2, TOOL_TEMPORARY_ERROR_EXHAUSTED, True),
        (3, None, False),
    ]


async def test_transient_errors_three_times_use_temporary_code():
    summary, _ = await _fail(FakeTool(TransientToolError()))

    assert (summary.failure_code, summary.attempt_count, summary.retry_exhausted) == (
        TOOL_TEMPORARY_ERROR_EXHAUSTED,
        3,
        True,
    )


async def test_per_attempt_timeout_counts_as_timeout():
    async def slow() -> None:
        await asyncio.sleep(1)

    calls = 0

    async def tool() -> None:
        nonlocal calls
        calls += 1
        await slow()

    sleeps = Sleeps()
    with pytest.raises(ToolFailure) as caught:
        await call_with_retry(
            "get_open_orders", STEP, tool, timeout_seconds=0.01, sleep=sleeps
        )

    assert calls == 3
    assert caught.value.summary.failure_code == TOOL_TIMEOUT_EXHAUSTED


async def test_permission_error_is_not_retried():
    tool = FakeTool(PermissionDeniedError("다른 고객사"))

    summary, sleeps = await _fail(tool)

    assert tool.calls == 1
    assert sleeps.waits == []
    assert (
        summary.status,
        summary.failure_code,
        summary.attempt_count,
        summary.retry_exhausted,
    ) == (AnalysisOutcome.FAILED, PERMISSION_DENIED, 1, False)


async def test_required_data_error_holds_with_its_own_message():
    tool = FakeTool(
        RequiredDataError(message="차단 검증 오류 NEGATIVE_QUANTITY WMS|SKU|2026-01-01")
    )

    summary, _ = await _fail(tool)

    assert tool.calls == 1
    # 정답 overlay E9 v1: ON_HOLD·VALIDATING·REQUIRED_DATA_INVALID·1·false, 판정한 쪽 문구 그대로
    assert (
        summary.status,
        summary.failed_step,
        summary.failure_code,
        summary.attempt_count,
        summary.retry_exhausted,
        summary.failure_message,
    ) == (
        AnalysisOutcome.ON_HOLD,
        AnalysisStep.VALIDATING,
        REQUIRED_DATA_INVALID,
        1,
        False,
        "차단 검증 오류 NEGATIVE_QUANTITY WMS|SKU|2026-01-01",
    )


async def test_unexpected_error_is_not_retried():
    tool = FakeTool(KeyError("sku"))

    summary, _ = await _fail(tool)

    assert tool.calls == 1
    assert (summary.failure_code, summary.attempt_count, summary.retry_exhausted) == (
        UNEXPECTED_TOOL_ERROR,
        1,
        False,
    )


async def test_permanent_error_after_one_transient_keeps_attempt_number():
    summary, _ = await _fail(FakeTool(TimeoutError(), PermissionDeniedError()))

    assert (summary.failure_code, summary.attempt_count, summary.retry_exhausted) == (
        PERMISSION_DENIED,
        2,
        False,
    )


async def test_inner_tool_failure_is_passed_up_unchanged():
    inner = hold_summary(REQUIRED_DATA_INVALID, "안쪽에서 보류")
    tool = FakeTool(ToolFailure(inner))

    summary, _ = await _fail(tool)

    assert tool.calls == 1
    assert summary is inner


async def test_broken_attempt_hook_does_not_stop_the_call():
    def broken(_: AttemptRecord) -> None:
        raise RuntimeError("추적 서버 장애")

    result = await call_with_retry(
        "get_operating_rules", STEP, FakeTool("ok"), on_attempt=broken
    )

    assert result == "ok"


# --- 실패 요약 값 규칙 -----------------------------------------------------------------------


def test_explanation_failure_sets_ck26_columns_and_keeps_engine_detail():
    summary = explanation_failure(
        "1:VALUE_MISMATCH@risk_summary#s1;2:MODEL_TIMEOUT@adapter"
    )

    columns = summary.columns()
    assert (
        columns["status"],
        columns["failed_step"],
        columns["failure_code"],
        columns["attempt_count"],
        columns["retry_exhausted"],
        columns["explanation_validation_status"],
        columns["final_explanation_json"],
    ) == (
        "FAILED",
        "VALIDATING_OUTPUT",
        EXPLANATION_GENERATION_FAILED,
        3,
        True,
        "FAILED",
        None,
    )
    assert "1:VALUE_MISMATCH@risk_summary#s1" in columns["failure_message"]
    # 리뷰(#28): 그대로 저장해도 되게 끝낼 때 바꿔야 하는 열을 모두 담는다(ck23·ck21·ck36·ck7)
    assert (
        columns["completed_status"],
        columns["result_class"],
        columns["dependency_scope"],
        columns["explanation_generated_at"],
    ) == ("FAILED", None, None, None)
    assert (
        "decision_signature" not in columns
    )  # ck37·ck40 이 막지 않으므로 바꾸지 않는다


def test_tool_failure_columns_close_the_run_without_explanation_keys():
    columns = hold_summary(REQUIRED_DATA_INVALID, "보류").columns()

    assert (columns["status"], columns["completed_status"]) == ("ON_HOLD", "ON_HOLD")
    assert (columns["result_class"], columns["dependency_scope"]) == (None, None)
    assert "explanation_validation_status" not in columns


async def test_hold_after_transient_error_keeps_real_attempt_count():
    # 리뷰(#28) 비차단 1: 시간 초과 1회 뒤 필수 자료 오류면 시도 2 (agentflow 7.1 "마지막 도구의 최종 시도 횟수")
    summary, _ = await _fail(FakeTool(TimeoutError(), RequiredDataError()))

    assert (summary.status, summary.attempt_count, summary.retry_exhausted) == (
        AnalysisOutcome.ON_HOLD,
        2,
        False,
    )


async def test_empty_backoff_retries_without_waiting():
    # 리뷰(#28) 비차단 3: 빈 backoff_seconds 는 IndexError 대신 기다리지 않고 재시도
    tool = FakeTool(TimeoutError(), "ok")
    sleeps = Sleeps()

    assert (
        await call_with_retry("t", STEP, tool, backoff_seconds=(), sleep=sleeps) == "ok"
    )
    assert (tool.calls, sleeps.waits) == (2, [])


def test_hold_summary_keeps_message_of_the_hold():
    """보류 문구는 판정한 쪽(엔진 입력 보류 #53 EngineHold) 것을 그대로 쓴다. 코드는 정답 E9 v1 과 같다"""
    message = "차단 검증 오류 NEGATIVE_QUANTITY WMS-EWR01|SKU-215|2026-01-01"

    summary = hold_summary(REQUIRED_DATA_INVALID, message)

    assert REQUIRED_DATA_INVALID == "REQUIRED_DATA_INVALID"
    assert summary.failure_message == message


_OK = {
    "status": AnalysisOutcome.FAILED,
    "failed_step": STEP,
    "failure_code": TOOL_TIMEOUT_EXHAUSTED,
    "attempt_count": 3,
    "retry_exhausted": True,
    "failure_message": "시간 초과",
}


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"status": AnalysisOutcome.READY_FOR_REVIEW}, ValueError),  # ck10
        ({"failed_step": "COMPARING"}, TypeError),  # ck12
        ({"attempt_count": 0}, ValueError),  # ck13
        ({"attempt_count": 4}, ValueError),  # ck13
        ({"attempt_count": 2}, ValueError),  # ck14 소진인데 2회
        ({"failure_message": ""}, ValueError),  # ck11
        (
            {"failure_code": EXPLANATION_GENERATION_FAILED},
            ValueError,
        ),  # ck26 단계가 VALIDATING_OUTPUT 이 아님
        (
            {"status": AnalysisOutcome.ON_HOLD},
            ValueError,
        ),  # ON_HOLD 인데 ANALYZING_IMPACT·도구 코드
    ],
)
def test_summary_rejects_values_the_ddl_rejects(change, error):
    with pytest.raises(error):
        FailureSummary(**{**_OK, **change})


# --- 실제 DB 행에 덮어써 analysis_runs CHECK 전부로 판정 (읽기 전용) ------------------------------------
# 리뷰(#28) 반영: 처음에는 ck10~14·ck26 만 봐서 completed_status(ck23)·result_class(ck21)·dependency_scope(ck36)
# 누락을 못 잡았다. 이제 모델의 CHECK 42개를 모두, 엔진 결과 값이 찬 실제 행 위에서 판정한다.

_CHECKS = {
    c.name.removeprefix("analysis_runs_"): str(c.sqltext)
    for c in AnalysisRun.__table__.constraints
    if isinstance(c, CheckConstraint) and c.name
}
_COLUMNS = {
    c.name: c.type.compile(dialect=postgresql.dialect())
    for c in AnalysisRun.__table__.columns
}


def test_model_has_all_42_checks():
    assert len(_CHECKS) == 42


async def _base_run_id(db, status: str) -> uuid.UUID:
    """엔진 결과 값(결과 분류·의존 범위·판정 서명)이 찬 완료 행. 진행 중 행에 엔진 값이 남은 최악의 경우를 대신한다."""
    return (
        await db.execute(
            text(
                "SELECT analysis_run_id FROM analysis_runs WHERE status = :s"
                " AND result_class IS NOT NULL AND dependency_scope IS NOT NULL"
                " AND decision_signature IS NOT NULL"
                " ORDER BY analysis_run_id LIMIT 1"
            ),
            {"s": status},
        )
    ).scalar_one()


async def _failing_checks(db, run_id: uuid.UUID, values: dict[str, Any]) -> list[str]:
    """실제 행에 values 를 덮어쓴 행에서 거부되는 CHECK 이름. CHECK 는 FALSE 일 때만 거부한다(NULL 통과)."""
    assert not set(values) - set(_COLUMNS), set(values) - set(_COLUMNS)
    params: dict[str, Any] = {}
    cols = []
    for name, sql_type in _COLUMNS.items():
        if name in values:
            value = values[name]
            if value is not None and sql_type.upper() in ("JSON", "JSONB"):
                value = json.dumps(value)
            params[f"v_{name}"] = value
            cols.append(f'CAST(:v_{name} AS {sql_type}) AS "{name}"')
        else:
            cols.append(f'r."{name}"')
    checks = ", ".join(
        f'({sql.replace(":", chr(92) + ":")}) IS NOT FALSE AS "{name}"'
        for name, sql in _CHECKS.items()
    )
    stmt = text(
        f"SELECT {checks} FROM (SELECT {', '.join(cols)} FROM analysis_runs r"
        " WHERE r.analysis_run_id = :run_id) AS t"
    ).bindparams(*(bindparam(k) for k in params), bindparam("run_id"))
    row = (await db.execute(stmt, {**params, "run_id": run_id})).mappings().one()
    return sorted(name for name, ok in row.items() if not ok)


async def _summaries() -> list[FailureSummary]:
    made = [
        explanation_failure("1:MODEL_TIMEOUT@adapter"),
        hold_summary(REQUIRED_DATA_INVALID, "보류"),
    ]
    for script in (
        (TimeoutError(),),
        (TransientToolError(),),
        (PermissionDeniedError(),),
        (KeyError("x"),),
        (TimeoutError(), PermissionDeniedError()),
        (TimeoutError(), RequiredDataError()),
    ):
        with pytest.raises(ToolFailure) as caught:
            await call_with_retry("t", STEP, FakeTool(*script), sleep=Sleeps())
        made.append(caught.value.summary)
    return made


@pytest.mark.parametrize("base_status", ["FINALIZED", "NO_RISK"])
async def test_every_summary_passes_all_checks_over_a_real_row(db_session, base_status):
    run_id = await _base_run_id(db_session, base_status)
    assert (
        await _failing_checks(db_session, run_id, {}) == []
    )  # 기준 행 자체는 통과(시험 장치 확인)

    summaries = await _summaries()
    assert len(summaries) == 8
    for summary in summaries:
        assert await _failing_checks(db_session, run_id, summary.columns()) == [], (
            summary
        )


async def test_checks_catch_missing_or_wrong_columns(db_session):
    # 음성: columns() 에서 열을 빼면(= 기준 행의 엔진 값이 남으면) 리뷰가 지적한 제약이 실제로 거부하는지
    run_id = await _base_run_id(db_session, "FINALIZED")
    tool = (await _summaries())[2].columns()  # 시간 초과 소진
    explain = explanation_failure().columns()

    def without(values: dict[str, Any], key: str) -> dict[str, Any]:
        return {k: v for k, v in values.items() if k != key}

    cases = {
        "ck23": without(tool, "completed_status"),
        "ck21": without(explain, "result_class"),
        "ck36": without(tool, "dependency_scope"),
        "ck7": without(explain, "explanation_generated_at"),
        "ck14": {**tool, "attempt_count": 2},
        "ck13": {**tool, "attempt_count": 4, "retry_exhausted": False},
        "ck26": {**explain, "failed_step": "ANALYZING_IMPACT"},
        "ck10": {
            **tool,
            "status": "READY_FOR_REVIEW",
            "completed_status": "READY_FOR_REVIEW",
        },
        "ck11": {**tool, "failure_message": None},
        "ck12": {**tool, "failed_step": "COMPARING"},
    }
    for expected, values in cases.items():
        assert expected in await _failing_checks(db_session, run_id, values), expected
