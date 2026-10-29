import uuid

from pydantic import BaseModel, Field

from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)


class AgentDryRunRequest(BaseModel):
    """빈 노드 그래프 실행 확인용 입력. 값을 주지 않으면 임의 UUID를 사용합니다."""

    analysis_run_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    company_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    delay_event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    operating_rule_set_id: uuid.UUID = Field(default_factory=uuid.uuid4)


class AgentDryRunResponse(BaseModel):
    analysis_run_id: uuid.UUID
    outcome: AnalysisOutcome
    last_step: AnalysisStep | None
    visited_nodes: list[str]
