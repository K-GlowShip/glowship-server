import asyncio
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from langchain_core.callbacks import BaseCallbackHandler

"""
    에이전트 실행 추적 (GLOW-124, agentflow 8절·8.1절 LangSmith 경계, docs/agent-flow/agent-tracing.md)
      - 실행 ID 하나로 분석 시작 → 노드·갈림길 → 도구 시도 → 설명 시도 → 종료 상태를 사건(TraceEvent)으로 남긴다.
      - 공식 기록은 업무 DB 다. 추적은 관찰·디버깅용이고, 추적이 실패하거나 느려도 분석은 그대로 간다(장애 독립성).
          · 분석 쪽은 사건을 줄에 넣기만 하고, 쓰기는 실행마다 하나인 쓰기 스레드가 한다.
          · 쓰기 예외는 세기만 하고 올리지 않는다.
      - 보내지 않는 것: 비밀번호·API 키·토큰, 프롬프트 전문, 원본 업무 데이터 전체. 상태는 키 이름과 안전한 값만 요약한다.
        주문 코드는 실행마다 다른 키로 HMAC 해 바꾸고(같은 실행 안에서만 같은 값), 이메일은 지운다.
      - LangSmith 전송은 기본 꺼짐이다. 외부 전송이 승인되기 전에는 전송기를 두지 않고 내부 로그(outbox)에만 쓴다.
        LangChain 자동 추적 환경 변수가 켜져 있으면 마스킹 없이 상태 전체가 나갈 수 있으므로 추적기를 만들 때 멈춘다.
      - 사건 ID 는 uuid5(실행 ID, 순번)라 같은 사건을 다시 보내도 중복이 생기지 않는다.
"""

logger = logging.getLogger(__name__)

EventKind = Literal[
    "RUN_START",
    "RUN_END",
    "RUN_ERROR",
    "NODE_START",
    "NODE_END",
    "NODE_ERROR",
    "ROUTE",
    "TOOL_ATTEMPT",
    "EXPLANATION_ATTEMPT",
    "MODEL_USAGE",
]
EventStatus = Literal["STARTED", "OK", "RETRY", "ERROR"]

# LangChain·LangSmith 가 이 값들 중 하나라도 켜져 있으면 그래프 상태를 마스킹 없이 자동으로 보낸다
AUTO_TRACING_ENV = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
)
_TRUTHY = {"1", "true", "yes", "on"}

# 그래프 상태 가운데 값까지 남겨도 되는 키(단계·결과 코드). 나머지는 키 이름·크기만 남긴다
SAFE_STATE_KEYS = frozenset(
    {"step", "outcome", "stopped_at", "has_at_risk_orders", "visited_nodes"}
)
# 값을 통째로 빼는 칸: 비밀번호·키·인증 토큰, 프롬프트 원문, 모델 입력 문서. 판 번호(prompt_version)·토큰 수(input_tokens)는 남긴다
_REMOVED_KEY = re.compile(
    r"password|api_?key|secret|authorization|(^|_)(access|refresh|auth|bearer|id)_token$|^token$"
    r"|^prompt$|prompt_text|rendered_prompt|full_prompt|input_document|raw_output",
    re.IGNORECASE,
)
# re.ASCII: Python 의 \b·\w 는 기본으로 한글도 단어 글자로 봐서, "…-0040이"처럼 조사가 붙으면 경계가 없어 안 가려졌다(PR #42 리뷰).
# 주문 코드는 날짜·번호 형식 밖의 키(B-ORD-ERR-0001 등)도 가린다.
_ORDER_CODE = re.compile(r"\b[A-Z]-ORD-[A-Z0-9]+(?:-[A-Z0-9]+)*\b", re.ASCII)
# 도메인 끝이 글자여야 이메일로 본다(판 번호 explain_ready_v1@1.0.0 은 이메일이 아님)
_EMAIL = re.compile(
    r"[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b", re.ASCII
)
_MAX_TEXT = 200
_MAX_ITEMS = 20


class TraceConfigError(Exception):
    """추적을 안전하게 켤 수 없는 설정(예: LangChain 자동 추적이 켜져 있음)."""


@dataclass(frozen=True)
class TraceEvent:
    event_id: uuid.UUID
    trace_id: uuid.UUID
    analysis_run_id: uuid.UUID
    company_id: uuid.UUID
    seq: int
    kind: EventKind
    name: str
    status: EventStatus
    at: str  # UTC ISO 8601
    attempt: int | None = None
    error_code: str | None = None
    elapsed_ms: int | None = None
    summary: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "trace_id": str(self.trace_id),
            "analysis_run_id": str(self.analysis_run_id),
            "company_id": str(self.company_id),
            "seq": self.seq,
            "kind": self.kind,
            "name": self.name,
            "status": self.status,
            "at": self.at,
            "attempt": self.attempt,
            "error_code": self.error_code,
            "elapsed_ms": self.elapsed_ms,
            "summary": self.summary,
        }


class TraceSink(Protocol):
    def write(self, event: TraceEvent) -> None: ...


class TraceMasker:
    """추적에 남길 값을 줄이고 가린다. 키는 실행마다 새로 만든다(같은 실행 안에서만 같은 가림 값)."""

    def __init__(self, key: bytes | None = None):
        self._key = key or secrets.token_bytes(32)

    def _order(self, match: re.Match) -> str:
        digest = hmac.new(
            self._key, match.group(0).encode(), hashlib.sha256
        ).hexdigest()
        return f"ORD#{digest[:8]}"

    def text(self, value: str) -> str:
        masked = _EMAIL.sub("[EMAIL]", _ORDER_CODE.sub(self._order, value))
        return masked if len(masked) <= _MAX_TEXT else masked[:_MAX_TEXT] + "…"

    def value(self, value: Any) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, uuid.UUID):
            return str(value)
        if hasattr(value, "value") and isinstance(value.value, str):  # StrEnum
            return value.value
        if isinstance(value, Mapping):
            return {
                str(k): ("[REMOVED]" if _removed_key(str(k)) else self.value(v))
                for k, v in list(value.items())[:_MAX_ITEMS]
            }
        if isinstance(value, list | tuple | set | frozenset):
            items = list(value)
            masked = [self.value(v) for v in items[:_MAX_ITEMS]]
            return (
                masked + [f"…(+{len(items) - _MAX_ITEMS})"]
                if len(items) > _MAX_ITEMS
                else masked
            )
        return f"<{type(value).__name__}>"

    def state(self, state: Any) -> dict[str, Any]:
        """그래프 상태(입력·출력)를 요약한다: 키 이름 전부 + 안전한 키의 값."""
        if not isinstance(state, Mapping):
            return {"type": type(state).__name__}
        summary: dict[str, Any] = {"keys": sorted(str(k) for k in state)}
        for key in SAFE_STATE_KEYS & set(state):
            summary[key] = self.value(state[key])
        return summary


def _removed_key(key: str) -> bool:
    return _REMOVED_KEY.search(key) is not None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunTracer:
    """실행 하나의 추적기. 사건은 줄에 넣고 쓰기 스레드가 sink 에 쓴다. 어떤 메서드도 예외를 올리지 않는다."""

    def __init__(
        self,
        analysis_run_id: uuid.UUID,
        company_id: uuid.UUID,
        sinks: Iterable[TraceSink],
        *,
        trace_id: uuid.UUID | None = None,
        masker: TraceMasker | None = None,
        clock: Callable[[], str] = _utc_now,
    ):
        self.analysis_run_id = analysis_run_id
        self.company_id = company_id
        self.trace_id = trace_id or analysis_run_id  # 서비스가 만든 UUID(실행 ID)
        self.masker = masker or TraceMasker()
        self.events: list[TraceEvent] = []
        self.sink_failures = 0
        self._sinks = list(sinks)
        self._clock = clock
        self._seq = 0
        self._lock = threading.Lock()
        self._writer = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="agent-trace"
        )
        self._pending: list[Future] = []

    # --- 사건 남기기 -----------------------------------------------------------------
    def emit(
        self,
        kind: EventKind,
        name: str,
        status: EventStatus,
        *,
        attempt: int | None = None,
        error_code: str | None = None,
        elapsed_ms: int | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> TraceEvent | None:
        try:
            with self._lock:
                self._seq += 1
                seq = self._seq
            event = TraceEvent(
                event_id=uuid.uuid5(self.trace_id, str(seq)),
                trace_id=self.trace_id,
                analysis_run_id=self.analysis_run_id,
                company_id=self.company_id,
                seq=seq,
                kind=kind,
                name=self.masker.text(name),
                status=status,
                at=self._clock(),
                attempt=attempt,
                error_code=self.masker.text(error_code) if error_code else None,
                elapsed_ms=elapsed_ms,
                summary=self.masker.value(dict(summary or {})),
            )
            self.events.append(event)
            self._pending.append(self._writer.submit(self._write, event))
            return event
        except Exception:  # 추적 실패가 분석을 멈추지 않게 한다
            self.sink_failures += 1
            logger.warning("추적 사건을 남기지 못했습니다(분석은 계속)", exc_info=True)
            return None

    def _write(self, event: TraceEvent) -> None:
        for sink in self._sinks:
            try:
                sink.write(event)
            except Exception:
                self.sink_failures += 1
                logger.warning(
                    "추적 기록을 쓰지 못했습니다: %s (분석은 계속)",
                    type(sink).__name__,
                    exc_info=True,
                )

    async def aclose(self, timeout: float = 2.0) -> bool:
        """이벤트 루프를 막지 않고 쓰기를 기다린다. async 코드(그래프 실행 뒤)에서는 이것을 쓴다.

        close() 는 동기라 루프에서 바로 부르면 쓰기가 밀렸을 때 루프 전체가 최대 timeout 초 멈춘다.
        그러면 같은 루프의 다른 실행의 도구가 시간 초과로 기록될 수 있다(PR #42 리뷰).
        """
        try:
            return await asyncio.to_thread(self.close, timeout)
        except Exception:
            logger.warning(
                "추적 쓰기 마무리에 실패했습니다(분석은 계속)", exc_info=True
            )
            return False

    def close(self, timeout: float = 2.0) -> bool:
        """쓰기를 기다린다(최대 timeout 초). 다 썼으면 True. 동기 코드용이다. async 코드에서는 aclose() 를 쓴다."""
        try:
            _, not_done = wait_futures(self._pending, timeout=timeout)
            self._writer.shutdown(wait=False, cancel_futures=False)
            return not not_done
        except Exception:
            logger.warning(
                "추적 쓰기 마무리에 실패했습니다(분석은 계속)", exc_info=True
            )
            return False

    # --- 재시도·설명 시도 연결 --------------------------------------------------------
    def tool_attempt(self, record: Any) -> None:
        """retry.call_with_retry(on_attempt=...) 에 넘기는 훅. record = AttemptRecord(GLOW-123)."""
        error_code = getattr(record, "error_code", None)
        will_retry = bool(getattr(record, "will_retry", False))
        status: EventStatus = (
            "OK" if error_code is None else ("RETRY" if will_retry else "ERROR")
        )
        self.emit(
            "TOOL_ATTEMPT",
            str(getattr(record, "tool_name", "?")),
            status,
            attempt=getattr(record, "attempt", None),
            error_code=error_code,
            elapsed_ms=getattr(record, "elapsed_ms", None),
            summary={"step": getattr(record, "step", None), "will_retry": will_retry},
        )

    def explanation_attempts(
        self, attempts: Iterable[Mapping[str, Any]], usage: Iterable[Any] = ()
    ) -> None:
        """엔진 explain.generator.generate() 결과의 attempts 를 사건으로 남긴다. 프롬프트·모델 출력 원문은 받지 않는다.

        모양이 엔진과 달라도(codes 가 None 등) 예외를 올리지 않는다(세기만 함, PR #42 리뷰).
        """
        try:
            usage_list = list(usage)
            for index, attempt in enumerate(attempts):
                raw_codes = attempt.get("codes") or []
                codes = [f"{code}@{loc}" for code, loc in raw_codes]
                summary: dict[str, Any] = {
                    "codes": codes,
                    "model": f"{attempt.get('model_provider')}/{attempt.get('model_id')}",
                    "prompt_version": f"{attempt.get('prompt_template_id')}@{attempt.get('prompt_template_version')}",
                }
                if index < len(usage_list) and usage_list[index] is not None:
                    summary["usage"] = usage_list[index]  # 모델 호출 토큰 수(NFR-008)
                self.emit(
                    "EXPLANATION_ATTEMPT",
                    "explain",
                    "ERROR" if codes else "OK",
                    attempt=attempt.get("retry_no"),
                    error_code=raw_codes[0][0] if raw_codes else None,
                    summary=summary,
                )
        except Exception:
            self.sink_failures += 1
            logger.warning(
                "설명 시도를 추적에 남기지 못했습니다(분석은 계속)", exc_info=True
            )

    def model_usage(self, feature: str, usage: Mapping[str, Any]) -> None:
        """모델 사용량 합계(NFR-008)를 사건 하나로 남긴다. feature 는 모델을 부른 기능 이름(예: "explain").

        usage 는 app/core/llm_usage.summarize_usage 의 결과다(숫자·정해진 코드·모델 이름뿐, 업무 값 없음).
        한 실행 안에서 모델을 부르는 기능마다 하나씩 남고, 실행 합계는 그 사건들의 합이다.
        """
        try:
            self.emit("MODEL_USAGE", feature, "OK", summary=usage)
        except Exception:
            self.sink_failures += 1
            logger.warning(
                "모델 사용량을 추적에 남기지 못했습니다(분석은 계속)", exc_info=True
            )

    def callback_handler(self) -> TraceCallbackHandler:
        return TraceCallbackHandler(self)


class TraceCallbackHandler(BaseCallbackHandler):
    """LangGraph 실행 사건 → 추적 사건. graph.ainvoke(config={"callbacks": [handler]}) 로 넘긴다.

    - 부모가 없는 체인 = 그래프 실행 전체(RUN_START·RUN_END·RUN_ERROR)
    - 이름이 metadata.langgraph_node 와 같고 'graph:step:' 태그가 있는 체인 = 노드(NODE_*)
    - 이름이 route_ 로 시작하는 체인 = 갈림길 결정(ROUTE, 결과 = 다음 노드 이름)
    """

    run_inline = (
        True  # 사건 순서를 지키고 스레드를 넘기지 않는다(하는 일은 줄에 넣기뿐)
    )
    raise_error = False

    def __init__(self, tracer: RunTracer):
        self.tracer = tracer
        self._open: dict[uuid.UUID, tuple[EventKind, str, float]] = {}

    def _classify(
        self, name: str | None, parent_run_id, tags, metadata
    ) -> tuple[str, str] | None:
        node = (metadata or {}).get("langgraph_node")
        if parent_run_id is None:
            return ("RUN", name or "graph")
        if (
            name
            and name == node
            and any(str(t).startswith("graph:step:") for t in tags or ())
        ):
            return ("NODE", name)
        if name and name.startswith("route_") and node:
            return ("ROUTE", name)
        return None

    def on_chain_start(
        self,
        serialized,
        inputs,
        *,
        run_id,
        parent_run_id=None,
        tags=None,
        metadata=None,
        **kwargs,
    ):
        try:
            found = self._classify(kwargs.get("name"), parent_run_id, tags, metadata)
            if found is None:
                return
            kind, name = found
            self._open[run_id] = (kind, name, time.perf_counter())
            if kind == "RUN":
                self.tracer.emit(
                    "RUN_START",
                    name,
                    "STARTED",
                    summary=self.tracer.masker.state(inputs),
                )
            elif kind == "NODE":
                summary = {"graph_step": (metadata or {}).get("langgraph_step")}
                self.tracer.emit("NODE_START", name, "STARTED", summary=summary)
        except Exception:
            logger.warning("추적 콜백 처리 실패(분석은 계속)", exc_info=True)

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        try:
            opened = self._open.pop(run_id, None)
            if opened is None:
                return
            kind, name, started = opened
            elapsed = int((time.perf_counter() - started) * 1000)
            if kind == "RUN":
                self.tracer.emit(
                    "RUN_END",
                    name,
                    "OK",
                    elapsed_ms=elapsed,
                    summary=self.tracer.masker.state(outputs),
                )
            elif kind == "NODE":
                self.tracer.emit(
                    "NODE_END",
                    name,
                    "OK",
                    elapsed_ms=elapsed,
                    summary=self.tracer.masker.state(outputs),
                )
            else:
                self.tracer.emit(
                    "ROUTE", name, "OK", elapsed_ms=elapsed, summary={"next": outputs}
                )
        except Exception:
            logger.warning("추적 콜백 처리 실패(분석은 계속)", exc_info=True)

    def on_chain_error(self, error, *, run_id, **kwargs):
        try:
            opened = self._open.pop(run_id, None)
            if opened is None:
                return
            kind, name, started = opened
            elapsed = int((time.perf_counter() - started) * 1000)
            event_kind: EventKind = "RUN_ERROR" if kind == "RUN" else "NODE_ERROR"
            # 오류 메시지는 남기지 않는다. 칸 이름 필터를 거치지 않는 자유 문장이라 접속 문자열·비밀번호·
            # 가리지 못한 업무 값이 섞일 수 있다(PR #42 리뷰). 오류 종류만 남기고, 내용은 서버 로그(예외)에서 본다.
            self.tracer.emit(
                event_kind,
                name,
                "ERROR",
                error_code=type(error).__name__,
                elapsed_ms=elapsed,
            )
        except Exception:
            logger.warning("추적 콜백 처리 실패(분석은 계속)", exc_info=True)


def auto_tracing_env_on(environ: Mapping[str, str] | None = None) -> list[str]:
    """켜져 있는 LangChain·LangSmith 자동 추적 환경 변수 이름."""
    env = os.environ if environ is None else environ
    return [
        name
        for name in AUTO_TRACING_ENV
        if str(env.get(name, "")).strip().lower() in _TRUTHY
    ]


def build_run_tracer(
    analysis_run_id: uuid.UUID,
    company_id: uuid.UUID,
    sinks: Iterable[TraceSink],
    *,
    environ: Mapping[str, str] | None = None,
) -> RunTracer:
    """실행 추적기를 만든다. LangChain 자동 추적이 켜져 있으면 TraceConfigError(마스킹 없는 외부 전송을 막음)."""
    enabled = auto_tracing_env_on(environ)
    if enabled:
        raise TraceConfigError(
            f"LangChain 자동 추적 환경 변수가 켜져 있습니다: {enabled}. 그래프 상태가 마스킹 없이 외부로 나갈 수 있어 추적을 시작하지 않습니다."
            " 외부 전송은 승인 뒤 전용 전송기로 켭니다(agentflow 8.1)"
        )
    return RunTracer(analysis_run_id, company_id, sinks)
