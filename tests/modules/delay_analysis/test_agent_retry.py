import asyncio
import json
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, bindparam, text

from app.modules.data_sync.application.validation import blocking_issues
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


def test_hold_code_matches_data_sync_blocking_issue():
    issue = blocking_issues.BlockingIssue("WMS|SKU|2026-01-01", "NEGATIVE_QUANTITY")

    summary = hold_summary(issue.failure_code, issue.failure_message)

    assert REQUIRED_DATA_INVALID == blocking_issues.REQUIRED_DATA_INVALID
    assert summary.failure_message == issue.failure_message


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


# --- 실제 DB CHECK 원문으로 판정 (읽기 전용) ----------------------------------------------------

_CHECKED = ("ck10", "ck11", "ck12", "ck13", "ck14", "ck26")
_CHECKS = {
    c.name.removeprefix("analysis_runs_"): str(c.sqltext)
    for c in AnalysisRun.__table__.constraints
    if isinstance(c, CheckConstraint)
    and c.name
    and c.name.removeprefix("analysis_runs_") in _CHECKED
}
_ROW = (
    "SELECT CAST(:status AS text) AS status, CAST(:failed_step AS text) AS failed_step,"
    " CAST(:failure_code AS text) AS failure_code, CAST(:attempt_count AS integer) AS attempt_count,"
    " CAST(:retry_exhausted AS boolean) AS retry_exhausted, CAST(:failure_message AS text) AS failure_message,"
    " CAST(:explanation_validation_status AS text) AS explanation_validation_status,"
    " CAST(:final_explanation_json AS jsonb) AS final_explanation_json"
)
_EMPTY = {
    "failed_step": None,
    "failure_code": None,
    "attempt_count": None,
    "retry_exhausted": None,
    "failure_message": None,
    "explanation_validation_status": None,
    "final_explanation_json": None,
}


async def _failing_checks(db, values: dict[str, Any]) -> list[str]:
    """CHECK 는 FALSE 일 때만 거부한다(NULL 은 통과). 거부되는 제약 이름을 돌려준다."""
    row = {**_EMPTY, **values}
    if row["final_explanation_json"] is not None:
        row["final_explanation_json"] = json.dumps(row["final_explanation_json"])
    failing = []
    for name in _CHECKED:
        stmt = text(
            f"SELECT ({_CHECKS[name]}) IS NOT FALSE FROM ({_ROW}) AS t"
        ).bindparams(*(bindparam(k) for k in row))
        if not (await db.execute(stmt, row)).scalar_one():
            failing.append(name)
    return failing


def test_all_checked_constraints_found_on_model():
    assert sorted(_CHECKS) == sorted(_CHECKED)


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
    ):
        with pytest.raises(ToolFailure) as caught:
            await call_with_retry("t", STEP, FakeTool(*script), sleep=Sleeps())
        made.append(caught.value.summary)
    return made


async def test_every_summary_we_make_passes_db_checks(db_session):
    summaries = await _summaries()
    assert len(summaries) == 7

    for summary in summaries:
        assert await _failing_checks(db_session, summary.columns()) == [], summary


async def test_db_checks_reject_wrong_rows(db_session):
    # 음성: FailureSummary 를 거치지 않고 틀린 값을 넣으면 DB 제약이 실제로 거부하는지
    ok = explanation_failure().columns()
    cases = {
        "ck14": {**ok, "failure_code": TOOL_TIMEOUT_EXHAUSTED, "attempt_count": 2},
        "ck13": {
            **ok,
            "failure_code": TOOL_TIMEOUT_EXHAUSTED,
            "attempt_count": 4,
            "retry_exhausted": False,
        },
        "ck26": {**ok, "failed_step": "ANALYZING_IMPACT"},
        "ck10": {
            **ok,
            "status": "READY_FOR_REVIEW",
            "failure_code": TOOL_TIMEOUT_EXHAUSTED,
        },
        "ck11": {**ok, "failure_message": None},
        "ck12": {
            **ok,
            "failed_step": "COMPARING",
            "failure_code": TOOL_TIMEOUT_EXHAUSTED,
        },
    }
    for expected, row in cases.items():
        assert expected in await _failing_checks(db_session, row), expected

    # 실패가 아닌 실행(5컬럼 모두 빔)은 통과
    assert await _failing_checks(db_session, {"status": "READY_FOR_REVIEW"}) == []
