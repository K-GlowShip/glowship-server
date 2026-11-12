import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)

"""
    도구 재시도와 실행 실패 요약 (agentflow_glowship.md 7·7.1절, docs/agent-flow/agent-retry.md)
      - 재시도 제한은 에이전트 실행 코드가 지킨다. 도구 한 번 부르기 = 최초 1회 + 재시도 최대 2회.
      - 재시도할 수 없는 오류(권한·필수 자료)는 한 번만 부르고 바로 멈춘다.
      - 실패하면 서비스가 analysis_runs 에 저장할 실패 요약 5컬럼(FailureSummary)을 만든다.
        값 규칙은 DDL analysis_runs_ck10~ck14·ck26 과 같다. 저장은 서비스가 한다(에이전트는 DB 에 쓰지 않음).
      - 설명 재생성(최대 3회)은 엔진 explain/generator.py 가 하므로 여기서 다시 하지 않고,
        그 결과를 실패 요약으로 옮기기만 한다(explanation_failure).
"""

MAX_ATTEMPTS = 3  # 최초 1 + 재시도 2 (agentflow 7.1, DDL ck13·ck14)

# --- failure_code ------------------------------------------------------------------------
# 근거: API 명세 v6.2 4.5 "FAILED(도구 실패, 예: TOOL_TIMEOUT_EXHAUSTED)", 정답 overlay E3 v2, 엔진 bundle_verify rt1
TOOL_TIMEOUT_EXHAUSTED = "TOOL_TIMEOUT_EXHAUSTED"
# 제안(설계 문서에 없음): 시간 초과가 아닌 일시 오류가 3회 이어진 경우
TOOL_TEMPORARY_ERROR_EXHAUSTED = "TOOL_TEMPORARY_ERROR_EXHAUSTED"
# 근거: agentflow 7.1 "재시도해도 같은 오류"
PERMISSION_DENIED = "PERMISSION_DENIED"
REQUIRED_DATA_INVALID = (
    "REQUIRED_DATA_INVALID"  # 정답 E9 v1, 엔진 입력 보류(#53 EngineHold)와 같은 값
)
MISSING_REQUIRED_DATA = "MISSING_REQUIRED_DATA"  # API 명세 4.5 ON_HOLD 사유
# 근거: DDL ck26
EXPLANATION_GENERATION_FAILED = "EXPLANATION_GENERATION_FAILED"
# 제안(설계 문서에 없음): 분류되지 않은 예외. 코드 오류일 수 있어 재시도하지 않는다
UNEXPECTED_TOOL_ERROR = "UNEXPECTED_TOOL_ERROR"

HOLD_CODES = frozenset({REQUIRED_DATA_INVALID, MISSING_REQUIRED_DATA})

logger = logging.getLogger(__name__)


# --- 도구가 던지는 오류 --------------------------------------------------------------------
class ToolError(Exception):
    """도구가 분류해서 던지는 오류. code 는 멈출 때 failure_code 가 된다."""

    retryable = False

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class TransientToolError(ToolError):
    """원천 응답 지연·일시 오류. 재시도한다. 3회 모두 실패하면 code(기본 TOOL_TEMPORARY_ERROR_EXHAUSTED)로 멈춘다."""

    retryable = True

    def __init__(
        self, message: str | None = None, code: str = TOOL_TEMPORARY_ERROR_EXHAUSTED
    ):
        super().__init__(code, message)


class PermissionDeniedError(ToolError):
    """허용된 고객사 범위 밖 등 권한 오류. 재시도하지 않고 FAILED."""

    def __init__(self, message: str | None = None):
        super().__init__(PERMISSION_DENIED, message)


class RequiredDataError(ToolError):
    """필수 자료 부족·검증 오류. 재시도하지 않고 ON_HOLD."""

    def __init__(self, code: str = REQUIRED_DATA_INVALID, message: str | None = None):
        if code not in HOLD_CODES:
            raise ValueError(f"보류 코드가 아닙니다: {code}")
        super().__init__(code, message)


# --- 실패 요약 5컬럼 -----------------------------------------------------------------------
@dataclass(frozen=True)
class FailureSummary:
    """
    analysis_runs 실패 요약 5컬럼 + 그때의 상태
      - FAILED·ON_HOLD 일 때만 만든다(ck10·ck11: 5개가 모두 차거나 모두 빔).
      - 만들 때 DDL 규칙을 먼저 확인한다(ck12·ck13·ck14·ck26). 어긋나면 ValueError.
      - columns() 는 실행을 실패·보류로 끝낼 때 analysis_runs 에서 바꿔야 하는 열을 모두 돌려준다.
        진행 중 행에 이 값을 덮어쓰면 analysis_runs CHECK 42개를 모두 통과한다(시험으로 확인).
    """

    status: AnalysisOutcome
    failed_step: AnalysisStep
    failure_code: str
    attempt_count: int
    retry_exhausted: bool
    failure_message: str

    def __post_init__(self) -> None:
        if self.status not in (AnalysisOutcome.FAILED, AnalysisOutcome.ON_HOLD):
            raise ValueError(f"실패 요약은 FAILED·ON_HOLD 만: {self.status}")  # ck10
        if not isinstance(self.failed_step, AnalysisStep):
            raise TypeError(f"허용되지 않은 단계: {self.failed_step}")  # ck12
        if not 1 <= self.attempt_count <= MAX_ATTEMPTS:
            raise ValueError(f"시도 횟수는 1~3: {self.attempt_count}")  # ck13
        if self.retry_exhausted and self.attempt_count != MAX_ATTEMPTS:
            raise ValueError("재시도 소진이면 시도 횟수는 3")  # ck14
        if not self.failure_code or not self.failure_message:
            raise ValueError("failure_code·failure_message 는 비울 수 없음")  # ck11
        if self.failure_code == EXPLANATION_GENERATION_FAILED and not (
            self.status is AnalysisOutcome.FAILED
            and self.failed_step is AnalysisStep.VALIDATING_OUTPUT
            and self.attempt_count == MAX_ATTEMPTS
            and self.retry_exhausted
        ):
            raise ValueError("설명 실패는 FAILED·VALIDATING_OUTPUT·3회·소진")  # ck26
        if self.status is AnalysisOutcome.ON_HOLD and (
            self.failed_step is not AnalysisStep.VALIDATING
            or self.failure_code not in HOLD_CODES
        ):
            raise ValueError(
                "ON_HOLD 는 VALIDATING·보류 코드만"
            )  # API 명세 4.5, 정답 E9 v1

    def columns(self) -> dict[str, Any]:
        """
        실행을 실패·보류로 끝낼 때 analysis_runs 에서 바꿔야 하는 열(서비스가 그대로 넣는다)
          - completed_status = status (ck23)
          - result_class·dependency_scope = NULL (ck21: 결과 분류는 READY_FOR_REVIEW·NO_RISK 때만,
            ck36: FAILED·ON_HOLD 는 의존 범위 없음. 엔진도 실패 때 scope=None)
          - decision_signature 는 바꾸지 않는다(ck37·ck40 이 FAILED·ON_HOLD 에 값을 막지 않음)
          - 설명 실패면 설명 검증 FAILED, 최종 설명·생성 시각 NULL (ck26·ck6·ck7)
        """
        values: dict[str, Any] = {
            "status": self.status.value,
            "completed_status": self.status.value,
            "result_class": None,
            "dependency_scope": None,
            "failed_step": self.failed_step.value,
            "failure_code": self.failure_code,
            "attempt_count": self.attempt_count,
            "retry_exhausted": self.retry_exhausted,
            "failure_message": self.failure_message,
        }
        if self.failure_code == EXPLANATION_GENERATION_FAILED:
            # ck26: 설명 검증 FAILED, 최종 설명 없음 (계산 결과 표는 저장됨, 추천 없음). ck7: 생성 시각도 없음
            values["explanation_validation_status"] = "FAILED"
            values["final_explanation_json"] = None
            values["explanation_generated_at"] = None
        return values


class ToolFailure(Exception):
    """
    call_with_retry 가 멈출 때 던진다. 노드는 이것을 받아 실행을 끝낸다(outcome·failure).
    detail 은 서버 로그에만 남길 자세한 사유다(Client 문구 failure_message 에는 넣지 않음, 이슈 #57 2번).
    """

    def __init__(self, summary: FailureSummary, detail: str | None = None):
        super().__init__(summary.failure_message)
        self.summary = summary
        self.detail = detail


# --- 시도 기록(추적 연결용) -------------------------------------------------------------------
@dataclass(frozen=True)
class AttemptRecord:
    """도구 한 번 시도의 기록. 호출별 시도 이력은 업무 DB 가 아니라 추적(LangSmith·내부 로그)에 남긴다(7.1)."""

    tool_name: str
    step: AnalysisStep
    attempt: int
    error_code: str | None  # 성공이면 None
    elapsed_ms: int
    will_retry: bool


# --- failure_message (한 곳에서 만든다. 문구를 바꾸려면 여기만 고친다) -------------------------------
# failure_message 는 Client 에게 보일 사유와 재실행 방법이다(agentflow 7.1). 내부 함수 이름·대조 값·예외 내용은 넣지 않는다(이슈 #57 2번).
# 예상하지 못한 오류(UNEXPECTED_TOOL_ERROR)는 원인과 상관없이 이 문장 하나(명세 v6.2 2절 문구, docs/frontend-api.md 실패 코드 표).
# 자세한 사유는 ToolFailure.detail 로 넘겨 서버 로그에만 남긴다(graph.with_failure_stop).
UNEXPECTED_ERROR_MESSAGE = "분석 중 예상하지 못한 오류로 멈췄습니다. 재분석을 요청하고, 다시 나오면 담당자에게 알려 주세요."

# 화면에 보일 도구 이름(agentflow 7.1 예 "판매량 조회가 시간 초과로 3번 모두 실패했습니다"). 설계 문서에는 T4 예만 있어 나머지는 같은 꼴로 정함.
# 키는 ToolName 값(tools/common.py)이다. retry 는 tools 를 import 하지 않으므로 문자열로 둔다.
TOOL_LABELS: dict[str, str] = {
    "get_delay_context": "지연 정보 조회",  # T1
    "get_inventory_snapshot": "재고 조회",  # T2
    "get_inbound_schedule": "입고 일정 조회",  # T3
    "get_sales_history": "판매량 조회",  # T4
    "get_open_orders": "미출고 주문 조회",  # T5
    "calculate_inventory_impact": "재고 영향 계산",  # T6
    "identify_at_risk_orders": "위험 주문 확인",  # T7
    "get_operating_rules": "운영 기준 조회",  # T8
    "check_option_feasibility": "대응안 가능 여부 확인",  # T9
    "calculate_option_outcomes": "대응안 결과 계산",  # T10
    "compare_options": "대응안 비교",  # T11
    "validate_agent_output": "결과 검증",  # T12
}
# 표에 없는 이름(시험용 가짜 도구 등)도 내부 이름을 보이지 않는다
UNKNOWN_TOOL_LABEL = "분석 도구"


def tool_label(tool_name: str) -> str:
    """화면에 보일 도구 이름 + 주격 조사(이/가). 예: "판매량 조회가", "재고 영향 계산이"."""
    label = TOOL_LABELS.get(tool_name, UNKNOWN_TOOL_LABEL)
    last = ord(label[-1]) - 0xAC00  # 한글 음절이면 받침 유무로 조사를 고른다
    return label + ("이" if 0 <= last < 11172 and last % 28 else "가")


def _tool_message(tool_name: str, code: str, attempts: int) -> str:
    if code == UNEXPECTED_TOOL_ERROR:
        return UNEXPECTED_ERROR_MESSAGE
    who = tool_label(tool_name)
    if code == TOOL_TIMEOUT_EXHAUSTED:
        return f"{who} 시간 초과로 {attempts}번 모두 실패했습니다. 잠시 뒤 재분석을 요청하세요."
    if code == PERMISSION_DENIED:
        return f"{who} 권한 오류로 멈췄습니다(재시도하지 않음). 접근 권한을 확인하세요."
    return f"{who} 일시 오류로 {attempts}번 모두 실패했습니다. 잠시 뒤 재분석을 요청하세요."


# --- 실패 요약 만들기 ---------------------------------------------------------------------
def hold_summary(code: str, message: str, attempt_count: int = 1) -> FailureSummary:
    """
    필수 자료 부족·검증 오류 보류. message 는 판정한 쪽의 문구를 그대로 쓴다
    (예: 엔진 입력 보류 #53 EngineHold 의 message, 정답 E9 v1 "차단 검증 오류 <코드> <키>").
    attempt_count 는 마지막 도구의 최종 시도 횟수다(agentflow 7.1). 일시 오류 뒤 보류되면 2·3 이 된다.
    """
    return FailureSummary(
        status=AnalysisOutcome.ON_HOLD,
        failed_step=AnalysisStep.VALIDATING,
        failure_code=code,
        attempt_count=attempt_count,
        retry_exhausted=False,
        failure_message=message,
    )


def explanation_failure(attempt_detail: str | None = None) -> FailureSummary:
    """
    설명 생성·검증 3회 실패(엔진 generator 결과 status = FAILED).
    attempt_detail 은 엔진 generator 의 failure_message("1:코드@위치;2:…")로, 그대로 덧붙인다.
    """
    message = "설명 생성·검증이 3번 모두 실패했습니다. 계산 결과는 남았지만 최종 선택 대상이 아닙니다. 재분석을 요청하세요."
    if attempt_detail:
        message = f"{message} (시도: {attempt_detail})"
    return FailureSummary(
        status=AnalysisOutcome.FAILED,
        failed_step=AnalysisStep.VALIDATING_OUTPUT,
        failure_code=EXPLANATION_GENERATION_FAILED,
        attempt_count=MAX_ATTEMPTS,
        retry_exhausted=True,
        failure_message=message,
    )


def _classify(error: Exception) -> tuple[bool, str]:
    """(재시도할지, 멈출 때의 failure_code)"""
    if isinstance(error, ToolError):
        return error.retryable, error.code
    if isinstance(error, TimeoutError):  # asyncio.TimeoutError 포함(3.11+)
        return True, TOOL_TIMEOUT_EXHAUSTED
    return False, UNEXPECTED_TOOL_ERROR


async def call_with_retry[T](
    tool_name: str,
    step: AnalysisStep,
    call: Callable[[], Awaitable[T]],
    *,
    timeout_seconds: float | None = None,
    backoff_seconds: Sequence[float] = (0.2, 0.5),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_attempt: Callable[[AttemptRecord], None] | None = None,
) -> T:
    """
    도구를 부르고, 일시 오류면 최대 2회 더 부른다.
      - 성공하면 결과를 돌려준다.
      - 멈추면 ToolFailure(실패 요약)를 던진다.
          · 재시도할 오류가 3회 이어짐 → FAILED, 시도 3, 소진 true
          · 재시도하지 않는 오류 → 그 시도 번호, 소진 false (권한 FAILED, 필수 자료 ON_HOLD)
      - timeout_seconds: 시도마다의 시간 제한. 넘으면 시간 초과로 보고 재시도한다.
      - backoff_seconds[i]: i+1 번째 실패 뒤 다시 부르기 전 기다리는 시간. 모자라면 마지막 값, 비었으면 기다리지 않는다.
      - on_attempt: 시도마다 부른다(추적 연결용). 여기서 난 오류는 분석을 멈추지 않는다.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            if timeout_seconds is None:
                result = await call()
            else:
                async with asyncio.timeout(timeout_seconds):
                    result = await call()
        except ToolFailure:
            raise  # 안쪽 도구 호출이 이미 멈춘 경우: 그 실패 요약을 그대로 올린다
        except Exception as error:  # 분류해서 실패 요약으로 바꾼다
            retryable, code = _classify(error)
            will_retry = retryable and attempt < MAX_ATTEMPTS
            _record(on_attempt, tool_name, step, attempt, code, started, will_retry)
            if will_retry:
                if backoff_seconds:
                    await sleep(
                        backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)]
                    )
                continue
            raise ToolFailure(
                _stop_summary(tool_name, step, code, attempt, error, retryable)
            ) from error
        _record(on_attempt, tool_name, step, attempt, None, started, False)
        return result
    raise AssertionError("도달하지 않음")  # pragma: no cover


def _stop_summary(
    tool_name: str,
    step: AnalysisStep,
    code: str,
    attempt: int,
    error: Exception,
    retryable: bool,
) -> FailureSummary:
    if code in HOLD_CODES:
        message = error.message if isinstance(error, ToolError) else str(error)
        return hold_summary(code, message, attempt)
    return FailureSummary(
        status=AnalysisOutcome.FAILED,
        failed_step=step,
        failure_code=code,
        attempt_count=attempt,
        retry_exhausted=retryable and attempt == MAX_ATTEMPTS,
        failure_message=_tool_message(tool_name, code, attempt),
    )


def _record(
    on_attempt: Callable[[AttemptRecord], None] | None,
    tool_name: str,
    step: AnalysisStep,
    attempt: int,
    error_code: str | None,
    started: float,
    will_retry: bool,
) -> None:
    if on_attempt is None:
        return
    record = AttemptRecord(
        tool_name=tool_name,
        step=step,
        attempt=attempt,
        error_code=error_code,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        will_retry=will_retry,
    )
    try:
        on_attempt(record)
    except Exception:  # 추적 장애는 분석에 영향을 주지 않는다(8.1)
        logger.warning(
            "도구 시도 기록 실패: %s %s회", tool_name, attempt, exc_info=True
        )
