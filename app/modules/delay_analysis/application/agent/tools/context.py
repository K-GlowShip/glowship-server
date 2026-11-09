import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from app.modules.delay_analysis.application.agent.retry import AttemptRecord

"""
    도구 실행 문맥 ToolContext (docs/agent-flow/agent-tool-interface.md 3절, GLOW-129)
      - 실행 하나에 하나. 노드가 도구를 부를 때 call_tool 에 넘기고, 도구 함수는 이것만 읽는다.
      - 엔진 입력(engine)은 실행마다 한 번 만든다(개발 1 build_engine_input, 저장소 읽기 고객사당 약 9.5초).
        도구는 저장소를 다시 읽거나 엔진 계산을 다시 하지 않는다.
      - DB 세션은 넣지 않는다. 도구가 DB 를 직접 읽으면 실행 입력 순번 뒤의 변경이 섞일 수 있다.
      - 엔진 입력·추적기의 실제 타입은 다른 작업이 만든다(엔진 입력 = 개발 1, 추적기 = GLOW-124).
        여기서는 도구·call_tool 이 쓰는 최소 모양(Protocol)만 정해 서로 import 하지 않게 한다.
"""


class EngineInputLike(Protocol):
    """개발 1 의 EngineInput 이 갖는 값 중 도구가 읽는 것 (인터페이스 문서 3절, DV1-5 11-06)"""

    analysis_run_id: uuid.UUID  # T1 입력이 이 실행의 값인지 확인(개발 1 조회 도구)
    delay_event_id: uuid.UUID
    base_date: date  # 기준일 D
    input_seq: int  # 실행 입력 순번
    snap: dict[str, Any]  # 엔진 snapshot.build 결과
    analysis: dict[
        str, Any
    ]  # 엔진 pipeline.analyze(snap) 결과 (build_engine_input 안에서 한 번 계산)
    store: Any  # 엔진 Store. 스냅샷에서 되돌릴 수 없는 원값(T4 정수 합계)만 읽는다


class ToolAttemptTracer(Protocol):
    """GLOW-124 RunTracer 중 call_tool 이 쓰는 것"""

    def tool_attempt(self, record: AttemptRecord) -> None: ...


@dataclass(frozen=True)
class ToolContext:
    company_id: uuid.UUID
    analysis_run_id: uuid.UUID
    base_date: date  # 기준일 D (엔진 입력 EngineInput.base_date. 그날 자료가 없으면 실행 날짜보다 이를 수 있음)
    input_seq: int  # 실행 입력 순번 (#21)
    engine: EngineInputLike
    tracer: ToolAttemptTracer | None = None  # 없으면 추적하지 않는다
