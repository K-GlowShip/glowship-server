import uuid
from datetime import datetime

from pydantic import Field, field_validator

from app.core.schemas import CamelModel

"""
    최종 선택 요청·응답 (API 19 POST /analysis-runs/{runId}/final-decision, API 명세 v6.2 4.6절)
      - reasonText 는 필수이고 앞뒤 공백을 뺀 뒤 1~600자(글자 수)이며, 뺀 값을 저장합니다 (C25).
      - 형식 오류는 422 VALIDATION_ERROR 입니다(명세는 400, 현재 구현은 422. D3).
"""

REASON_MAX_LENGTH = 600


class FinalDecisionCreateRequest(CamelModel):
    response_option_id: uuid.UUID = Field(
        description="최종 선택할 대응안. 같은 실행의 FEASIBLE 대응안이어야 함 (C24)"
    )
    reason_text: str = Field(
        description=f"선택 이유. 앞뒤 공백을 뺀 뒤 1~{REASON_MAX_LENGTH}자 (C25)",
        examples=["재고 영향이 가장 적고 추가 비용이 없습니다."],
    )

    @field_validator("reason_text")
    @classmethod
    def strip_reason(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("선택 이유는 비우거나 공백만으로 쓸 수 없습니다.")
        if len(stripped) > REASON_MAX_LENGTH:
            raise ValueError(f"선택 이유는 {REASON_MAX_LENGTH}자 이하여야 합니다.")
        return stripped


class FinalDecisionCreateResponse(CamelModel):
    final_decision_id: uuid.UUID
    decided_at: datetime = Field(
        description="선택 시각 (UTC, 판단 시각). 데모(가상 시각)에서는 고객사의 가장 최근 수집 시작 시각, 아니면 서버 시각"
    )
    checked_seq: int = Field(description="선택 시점에 확인한 고객사의 최대 적용 순번")
    status: str = Field(description="실행 상태. 항상 FINALIZED", examples=["FINALIZED"])
