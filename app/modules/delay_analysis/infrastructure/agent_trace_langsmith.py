import logging
import queue
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.modules.delay_analysis.application.agent.tracing import TraceEvent

"""
    LangSmith 추적 전송기 (agentflow 8.1, docs/agent-flow/agent-tracing.md 3절)
      - 추적기(RunTracer)가 이미 가린 사건(TraceEvent)만 받는다. 그래프 상태·프롬프트·모델 출력 원문은 여기까지 오지 않는다.
        LangChain 자동 추적(LANGSMITH_TRACING 등)은 여전히 막는다(가리지 않은 상태가 나가므로, tracing.build_run_tracer).
      - 실행 하나의 사건을 모았다가 끝(RUN_END·RUN_ERROR, 또는 추적기를 닫을 때)에 LangSmith 실행 나무로 바꿔 한 번에 보낸다.
          · 맨 위 실행 id = 추적 ID(= 분석 실행 ID), 아래 실행 id = 사건 ID(uuid5) → 다시 보내도 중복이 생기지 않는다.
          · 노드 = 맨 위 아래 체인, 도구 시도·설명 시도·갈림길·모델 사용량 = 그때 열린 노드(없으면 맨 위) 아래 잎.
          · 메타데이터 analysis_run_id·company_id·kind·seq·attempt·status 로 묶어 본다.
      - 실행기는 이 전송기와 내부 로그 싱크(TraceOutboxSink)를 함께 둔다: 사건은 내부 로그에 바로 다 남고(서버가 죽어도),
        LangSmith 에는 실행 끝에 따로 보낸다(사용자 결정 11-13). 그래서 실패해도 다시 쓸 것이 없다.
      - 보내기는 이 전송기의 데몬 스레드가 한다(분석·추적기·서버 종료가 기다리지 않음). 실패는 세고 경고만 남긴다.
      - 내부 로그에서 실패분을 다시 보내는 일은 아직 없다(후속 — 실패 경고의 실행 ID 로 찾음).
"""

logger = logging.getLogger(__name__)

ROOT_RUN_NAME = "delay_analysis"
_END_KINDS = frozenset({"RUN_END", "RUN_ERROR"})
_LEAF_RUN_TYPE = {"TOOL_ATTEMPT": "tool", "EXPLANATION_ATTEMPT": "llm"}
# 끝 사건이 오지 않은 채 닫힌 실행·노드(취소·시간 초과 등)에 붙이는 오류 표시
UNFINISHED = "TRACE_UNFINISHED"


class TraceSendError(RuntimeError):
    """LangSmith 로 보내지 못했다(같은 사건은 내부 로그에 남겼거나 남길 곳이 없음)."""


class RunBatchClient(Protocol):
    """langsmith.Client 중 쓰는 부분."""

    def batch_ingest_runs(self, *, create: Sequence[dict[str, Any]]) -> None: ...


def _time(at: str) -> datetime:
    return datetime.fromisoformat(at)  # "…Z" 도 읽음(3.11+)


def _dotted(start: datetime, run_id: uuid.UUID) -> str:
    # langsmith.run_trees._create_current_dotted_order 와 같은 모양(시각 + 실행 id)
    return start.strftime("%Y%m%dT%H%M%S%fZ") + str(run_id)


def _metadata(event: TraceEvent) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "analysis_run_id": str(event.analysis_run_id),
        "company_id": str(event.company_id),
        "kind": event.kind,
        "seq": event.seq,
        "status": event.status,
    }
    if event.attempt is not None:
        meta["attempt"] = event.attempt
    if event.elapsed_ms is not None:
        meta["elapsed_ms"] = event.elapsed_ms
    return meta


def events_to_runs(events: Iterable[TraceEvent], project: str) -> list[dict[str, Any]]:
    """실행 하나의 사건 → LangSmith 실행(dict) 목록. 첫 줄이 맨 위 실행이다. 사건이 없으면 빈 목록."""
    ordered = sorted(events, key=lambda e: e.seq)
    if not ordered:
        return []
    first = ordered[0]
    trace_id = first.trace_id
    last_at = max(_time(e.at) for e in ordered)

    def new_run(
        run_id: uuid.UUID,
        name: str,
        run_type: str,
        start: datetime,
        parent: dict[str, Any] | None,
        event: TraceEvent,
    ) -> dict[str, Any]:
        if parent is not None:
            start = max(
                start, parent["start_time"]
            )  # 자식이 부모보다 먼저 시작하지 않게(시각은 밀리초 단위)
        dotted = _dotted(start, run_id)
        return {
            "id": run_id,
            "trace_id": trace_id,
            "parent_run_id": None if parent is None else parent["id"],
            "dotted_order": dotted
            if parent is None
            else f"{parent['dotted_order']}.{dotted}",
            "name": name,
            "run_type": run_type,
            "start_time": start,
            "end_time": None,
            "inputs": {},
            "outputs": None,
            "error": None,
            "session_name": project,
            "tags": [event.kind],
            "extra": {"metadata": _metadata(event)},
        }

    root = new_run(
        trace_id, ROOT_RUN_NAME, "chain", min(_time(e.at) for e in ordered), None, first
    )
    root["extra"]["metadata"].pop("seq", None)
    root["tags"] = ["analysis_run"]
    runs = [root]
    open_nodes: list[dict[str, Any]] = []

    for event in ordered:
        at = _time(event.at)
        kind = event.kind
        if kind == "RUN_START":
            root["inputs"] = dict(event.summary)
            root["extra"]["metadata"]["graph"] = event.name
        elif kind in _END_KINDS:
            root["end_time"] = at
            root["outputs"] = dict(event.summary)
            if kind == "RUN_ERROR":
                root["error"] = event.error_code or "ERROR"
            root["extra"]["metadata"]["status"] = event.status
        elif kind == "NODE_START":
            node = new_run(event.event_id, event.name, "chain", at, root, event)
            node["inputs"] = dict(event.summary)
            runs.append(node)
            open_nodes.append(node)
        elif kind in ("NODE_END", "NODE_ERROR"):
            node = next(
                (n for n in reversed(open_nodes) if n["name"] == event.name), None
            )
            if node is None:  # 시작을 못 본 노드 끝: 잎으로 남긴다
                node = new_run(event.event_id, event.name, "chain", at, root, event)
                runs.append(node)
            else:
                open_nodes.remove(node)
            node["end_time"] = at
            node["outputs"] = dict(event.summary)
            if kind == "NODE_ERROR":
                node["error"] = event.error_code or "ERROR"
            node["extra"]["metadata"]["status"] = event.status
        else:  # ROUTE·TOOL_ATTEMPT·EXPLANATION_ATTEMPT·MODEL_USAGE: 끝난 뒤 남는 사건 하나 = 잎 하나
            parent = open_nodes[-1] if open_nodes else root
            start = at - timedelta(milliseconds=event.elapsed_ms or 0)
            leaf = new_run(
                event.event_id,
                event.name,
                _LEAF_RUN_TYPE.get(kind, "chain"),
                start,
                parent,
                event,
            )
            leaf["end_time"] = max(at, leaf["start_time"])
            leaf["outputs"] = dict(event.summary)
            if event.status in ("ERROR", "RETRY"):
                leaf["error"] = event.error_code or event.status
            runs.append(leaf)

    for run in runs:  # 끝 사건 없이 닫힌 실행·노드
        if run["end_time"] is None:
            run["end_time"] = max(last_at, run["start_time"])
            run["error"] = run["error"] or UNFINISHED
    return runs


class LangSmithTraceSink:
    """여러 실행이 같이 쓰는 전송기(실행기 하나에 하나). 실행마다 사건을 모았다가 끝에서 한 번 보낸다.

    - 보내기는 이 전송기의 데몬 스레드 하나가 한다(줄에 넣고 바로 돌아감). 그래서 추적기 쓰기 스레드도, 서버 종료도
      LangSmith 응답을 기다리지 않는다. 종료 때 아직 못 보낸 것은 버려지지만 같은 사건이 내부 로그에 이미 있다
      (실행기가 내부 로그 싱크를 함께 둠, 사용자 결정 11-13). 응답 없는 주소에 2분 넘게 묶이던 문제(검토 11-13) 대응.
    - 실패는 세고(failed_traces) 경고 한 줄(실행 ID·오류 종류)만 남긴다. 분석·추적기에는 알리지 않는다.
    """

    def __init__(self, client: RunBatchClient | None, project: str):
        self._client = client
        self.project = project
        self.sent_traces = 0
        self.failed_traces = 0
        self._buffers: dict[uuid.UUID, list[TraceEvent]] = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        # 줄의 항목 = 보낼 사건 묶음, 또는 flush 표지(threading.Event — 앞의 것을 다 처리한 뒤 세움)
        self._queue: queue.Queue[list[TraceEvent] | threading.Event] = queue.Queue()
        self._sender = threading.Thread(
            target=self._run_sender, name="agent-trace-langsmith", daemon=True
        )
        self._sender.start()

    @classmethod
    def from_settings(
        cls,
        *,
        api_key: str,
        project: str,
        endpoint: str | None = None,
        client_factory: Callable[..., RunBatchClient] | None = None,
        timeout_ms: tuple[int, int] = (2_000, 5_000),
    ) -> LangSmithTraceSink:
        """langsmith.Client 로 만든다.

        자동 묶음 전송을 끄고(실패를 여기서 알기 위해), HTTP 단계 재시도를 끄고(urllib3 Retry total=0 — 기본은 3번에
        클라이언트 재시도 3번이 곱해져 응답 없는 주소면 2분 넘게 걸렸음), 실행 환경 정보(호스트 등)는 보내지 않는다.
        """
        sink = cls(None, project)
        if client_factory is None:
            from langsmith import Client as client_factory  # 켰을 때만 불러온다
        from urllib3.util import Retry

        sink._client = client_factory(
            api_url=endpoint,
            api_key=api_key,
            auto_batch_tracing=False,
            omit_traced_runtime_info=True,
            timeout_ms=timeout_ms,
            retry_config=Retry(total=0, raise_on_status=False),
            tracing_error_callback=sink._on_client_error,
        )
        return sink

    def _on_client_error(self, error: Exception) -> None:
        # langsmith.Client 는 전송 오류를 올리지 않고 이 콜백으로만 알린다(같은 스레드에서 부름)
        errors = getattr(self._local, "errors", None)
        if errors is not None:
            errors.append(error)

    def write(self, event: TraceEvent) -> None:
        with self._lock:
            self._buffers.setdefault(event.trace_id, []).append(event)
        if event.kind in _END_KINDS:
            self.end_trace(event.trace_id)

    def end_trace(self, trace_id: uuid.UUID) -> None:
        """모은 사건을 보낼 줄에 넣는다. 끝 사건 뒤에 다시 불러도(추적기 닫기) 넣을 것이 없으면 아무것도 하지 않는다."""
        with self._lock:
            events = self._buffers.pop(trace_id, None)
        if events:
            self._queue.put(events)

    def flush(self, timeout: float = 10.0) -> bool:
        """줄에 넣은 것을 다 보낼 때까지 기다린다(시험·도구용). 다 보냈으면 True."""
        done = threading.Event()
        self._queue.put(done)
        return done.wait(timeout)

    def _run_sender(self) -> None:
        while True:
            item = self._queue.get()
            if isinstance(item, threading.Event):
                item.set()
                continue
            events = item
            try:
                self._send(events)
                self.sent_traces += 1
            # 무엇이 나든 보내는 스레드는 살아 있어야 한다. 오류 문장은 남기지 않는다(주소·응답 본문이 섞일 수 있음, 종류만)
            except Exception as error:  # noqa: BLE001
                self.failed_traces += 1
                logger.warning(
                    "LangSmith 로 추적을 보내지 못했습니다: 실행 %s, 사건 %d건, %s (내부 로그에는 남아 있음 — AGENT_TRACE_DIR)",
                    events[0].trace_id,
                    len(events),
                    type(error).__name__,
                )

    def _send(self, events: list[TraceEvent]) -> None:
        if self._client is None:
            raise TraceSendError("LangSmith 클라이언트가 없습니다")
        runs = events_to_runs(events, self.project)
        self._local.errors = []
        try:
            self._client.batch_ingest_runs(create=runs)
            errors = self._local.errors
        finally:
            self._local.errors = None
        if errors:
            raise errors[0]
