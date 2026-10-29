import operator
import uuid
from enum import StrEnum
from typing import Annotated, Any, TypedDict

"""
    지연 분석 LangGraph 상태 정의 (agentflow_glowship.md 3·5절)
      - 서비스가 확정한 입력(실행 ID·고객사·지연 이벤트·운영 기준 버전)은 에이전트가 바꾸지 않습니다.
      - 각 노드는 자기 단계의 결과 필드만 채워 반환하고, 업무 DB에는 쓰지 않습니다(저장은 서비스 책임).
"""


class AnalysisStep(StrEnum):
    """에이전트 실행 단계 (analysis_runs.status 의 진행 중 값과 동일)"""

    VALIDATING = "VALIDATING"
    ANALYZING_IMPACT = "ANALYZING_IMPACT"
    ANALYZING_OPTIONS = "ANALYZING_OPTIONS"
    VALIDATING_OUTPUT = "VALIDATING_OUTPUT"


class AnalysisOutcome(StrEnum):
    """에이전트가 서비스에 반환하는 종료 상태"""

    READY_FOR_REVIEW = "READY_FOR_REVIEW"  # 검증된 분석 결과
    NO_DELAY = "NO_DELAY"  # 분석 준비: 지연 조건 해소
    NO_RISK = "NO_RISK"  # 재고 영향: 위험 주문 없음
    ON_HOLD = "ON_HOLD"  # 분석 준비: 필수 자료 부족·검증 오류
    FAILED = "FAILED"  # 재시도 후에도 도구 실패
    STALE = "STALE"  # 노드 경계에서 서비스가 STALE로 바꾼 것을 확인해 중단


class AnalysisState(TypedDict, total=False):
    # 서비스 입력 (5.5절)
    analysis_run_id: uuid.UUID
    company_id: uuid.UUID
    delay_event_id: uuid.UUID
    operating_rule_set_id: uuid.UUID

    # 진행 상태
    step: AnalysisStep
    visited_nodes: Annotated[list[str], operator.add]  # 실행된 노드 순서 (확인·추적용)

    # 단계별 결과 (현재 뼈대에서는 빈 값)
    delay_context: dict[str, Any]
    impact_result: dict[str, Any]
    has_at_risk_orders: bool
    option_results: dict[str, Any]
    comparison: dict[str, Any]
    explanation: dict[str, Any]

    # 종료
    outcome: AnalysisOutcome
    stopped_at: AnalysisStep  # STALE 등으로 중단한 단계
