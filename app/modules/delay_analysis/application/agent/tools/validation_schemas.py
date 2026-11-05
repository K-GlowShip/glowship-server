from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from app.modules.delay_analysis.application.agent.tools.common import (
    Sha256Hex,
    ToolId,
    ToolModel,
    UtcTimestamp,
)

"""
    T12 validate_agent_output 입력·출력 모델 (docs/agent-flow/tool-contracts.md 8절, GLOW-127)
      - 검증 규칙은 데이터 팀 엔진 explain.validator 를 그대로 쓴다. 이 모델은 함수 경계에서 주고받는 값만 정한다.
      - 기대 메타데이터는 모델 출력에서 읽지 않는다. 서버가 아는 값(실행 ID·판정 서명·모델·프롬프트·생성 시각·시도 번호)으로
        만들어 넘기고, 검증기가 출력의 메타데이터와 맞춰 본다(엔진 generator 와 같은 방식).
"""

MAX_EXPLANATION_ATTEMPTS = (
    3  # 엔진 generator.MAX_ATTEMPTS, agentflow 7.1 (시험이 엔진 값과 같은지 확인)
)


class AgentOutputStatus(StrEnum):
    """T12 판정 (agentflow 4절 "통과·설명 재요청·실패")"""

    PASSED = "PASSED"  # 오류 없음
    RETRY = "RETRY"  # 오류가 있고 아직 3번째 시도가 아님 → 설명 재요청
    FAILED = (
        "FAILED"  # 3번째 시도에도 오류 → 설명 생성 실패(EXPLANATION_GENERATION_FAILED)
    )


class ExplanationErrorCode(StrEnum):
    """설명 검증 사유 코드 (엔진 explain.validator 머리말, R-02′ 1.12)"""

    SCHEMA_INVALID = "SCHEMA_INVALID"
    SIGNATURE_HASH_MISMATCH = "SIGNATURE_HASH_MISMATCH"
    EVIDENCE_UNRESOLVED = "EVIDENCE_UNRESOLVED"
    VALUE_MISMATCH = "VALUE_MISMATCH"
    UNREFERENCED_VALUE = "UNREFERENCED_VALUE"
    OPTION_SET_MISMATCH = "OPTION_SET_MISMATCH"
    RECOMMENDATION_MISMATCH = "RECOMMENDATION_MISMATCH"
    FORBIDDEN_EXPRESSION = "FORBIDDEN_EXPRESSION"
    LENGTH_EXCEEDED = "LENGTH_EXCEEDED"
    LANGUAGE_INVALID = "LANGUAGE_INVALID"
    LEAKAGE_DETECTED = "LEAKAGE_DETECTED"
    # 생성기가 모델 호출 실패에 붙이는 코드(검증기는 내지 않음). 시도 기록에 나온다
    MODEL_CALL_ERROR = "MODEL_CALL_ERROR"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"


class ExplanationMetadata(ToolModel):
    """설명 출력이 가져야 할 메타데이터 (explanation.v1 의 메타 키)"""

    schema_version: Literal["explanation.v1"]
    language: Literal["ko"]
    analysis_run_id: ToolId
    decision_signature_hash: Sha256Hex
    model_provider: StrictStr
    model_id: StrictStr
    prompt_template_id: StrictStr
    prompt_template_version: StrictStr
    prompt_template_sha256: Sha256Hex
    generated_at: UtcTimestamp
    retry_no: Annotated[StrictInt, Field(ge=1, le=MAX_EXPLANATION_ATTEMPTS)]


class ValidateAgentOutputInput(ToolModel):
    input_document: dict[
        str, Any
    ]  # 설명 입력 문서 explanation_input.v1 (엔진 explain.input_doc.build)
    output: StrictStr  # 모델 출력 원문(JSON 문자열). 검증 뒤 실패 원문은 어디에도 남기지 않는다
    expected_metadata: ExplanationMetadata
    canary: (
        StrictStr | None
    )  # 그 시도의 프롬프트에 넣은 확인값. 출력에 나오면 유출. 모르면 null(유출 검사 1개를 건너뜀)

    @field_validator("input_document")
    @classmethod
    def _input_document_version(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("schema_version") != "explanation_input.v1":
            raise ValueError("input_document 는 explanation_input.v1 이어야 합니다")
        return value

    @model_validator(mode="after")
    def _metadata_matches_document(self) -> ValidateAgentOutputInput:
        """기대 메타데이터의 실행·판정 서명이 입력 문서와 다르면 연결 오류다(검증 결과가 아니라 예외)."""
        run = self.input_document.get("run") or {}
        meta = self.expected_metadata
        if meta.analysis_run_id != run.get("analysis_run_id"):
            raise ValueError(
                "expected_metadata.analysis_run_id 가 입력 문서와 다릅니다"
            )
        if meta.decision_signature_hash != run.get("decision_signature_sha256"):
            raise ValueError(
                "expected_metadata.decision_signature_hash 가 입력 문서와 다릅니다"
            )
        return self


class OutputValidationError(ToolModel):
    code: ExplanationErrorCode
    location: (
        StrictStr  # 검증기 위치(예: risk_summary#s1, evidence:E3). 원문은 넣지 않는다
    )


class AgentOutputValidation(ToolModel):
    status: AgentOutputStatus
    errors: list[OutputValidationError]  # 검증기가 낸 순서. PASSED 이면 빈 목록

    @model_validator(mode="after")
    def _status_matches_errors(self) -> AgentOutputValidation:
        if (self.status is AgentOutputStatus.PASSED) != (not self.errors):
            raise ValueError(
                "PASSED 는 오류가 없을 때만, RETRY·FAILED 는 오류가 있을 때만입니다"
            )
        return self
