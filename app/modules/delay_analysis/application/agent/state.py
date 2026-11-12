import operator
import uuid
from enum import StrEnum
from typing import Annotated, Any, TypedDict

from app.modules.delay_analysis.application.agent.tools.impact_schemas import (
    AtRiskOrders,
    InventoryImpact,
)
from app.modules.delay_analysis.application.agent.tools.lookup_schemas import (
    DelayContext,
    InboundSchedule,
    InventorySnapshot,
    OpenOrders,
    OperatingRules,
    SalesHistory,
)
from app.modules.delay_analysis.application.agent.tools.option_schemas import (
    OptionFeasibility,
    OptionOutcomes,
    OptionRanking,
)

"""
    지연 분석 LangGraph 상태 정의 (agentflow_glowship.md 3·5절)
      - 서비스가 확정한 입력(실행 ID·고객사·지연 이벤트·운영 기준 버전)은 에이전트가 바꾸지 않습니다.
      - 각 노드는 자기 단계의 결과 필드만 채워 반환하고, 업무 DB에는 쓰지 않습니다(저장은 서비스 책임).
      - 결과 필드는 도구 출력 모델 그대로입니다(계약 10). 서비스는 그래프가 끝난 상태를 받아 결과 저장 함수에 넘깁니다
        (docs/agent-flow/agent-tool-interface.md 7절). 실행 도구(등록표·엔진 입력)는 상태에 두지 않습니다(runtime.py).
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
    NO_RISK = "NO_RISK"  # 재고 영향: 위험 주문·예측 부족 없음
    ON_HOLD = "ON_HOLD"  # 분석 준비: 필수 자료 부족·검증 오류
    FAILED = "FAILED"  # 재시도 후에도 도구 실패, 설명 3회 실패
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

    # 분석 준비
    # 지연 해소 판정(엔진 입력 judge_no_delay 결과: 판정 서명·SHA-256) → NO_DELAY
    no_delay: Any
    # 데이터 경고(이슈 #122): 엔진 입력(보류 포함)이 정한 값을 그대로 나른다. 저장이 analysis_runs.data_warnings 에 쓴다.
    # 지연 해소(NO_DELAY)는 엔진 입력을 만들지 않아 이 키가 없다(저장하지 않음 = 응답 빈 목록)
    data_warnings: list[dict[str, Any]]

    # 조정 에이전트 (T1)
    delay_context: DelayContext

    # 재고 영향 분석 에이전트 (T2·T3·T4·T5·T8 → T7 → T6)
    inventory_snapshot: InventorySnapshot
    inbound_schedule: InboundSchedule
    sales_history: SalesHistory
    open_orders: OpenOrders
    operating_rules: OperatingRules
    at_risk_orders: AtRiskOrders
    inventory_impact: InventoryImpact
    has_at_risk_orders: bool  # T7 에 위험 주문이 있는지(추적 안전 키). 종료 갈림은 T6 completed_status 로 정한다

    # 대응안 분석 에이전트 (T2·T8 다시 조회 → T9 → T10)
    option_feasibility: OptionFeasibility
    option_outcomes: OptionOutcomes

    # 대응안 비교 (T11 → 판정 서명)
    option_ranking: OptionRanking
    decision: Any  # 판정 서명·의존 범위(decision_of 결과). NO_RISK·READY 에 있다

    # 추천 근거 설명 → 결과 검증 (T12)
    explanation: Any  # 통과한 설명(설명 생성 ExplanationPassed)
    # 설명 실패 때 모델·프롬프트 판(model_name·prompt_version) 출처
    explanation_failed: Any

    # 종료
    outcome: AnalysisOutcome
    # FAILED·ON_HOLD 의 retry.FailureSummary(실패 요약 5컬럼 재료, #28). retry 가 이 모듈을 import 해서 타입은 Any
    failure: Any
    stopped_at: AnalysisStep  # STALE 등으로 중단한 단계
