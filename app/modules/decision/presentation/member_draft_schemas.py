import uuid
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from app.core.schemas import CamelModel

"""
    의견·선택 초안 저장 요청·응답 (API 18 POST /analysis-runs/{runId}/drafts, API 명세 v6.2 4.6절)
      - 대응안(responseOptionId)과 의견(opinionText) 둘 다 선택이지만 둘 중 하나는 있어야 합니다.
      - 의견은 앞뒤 공백을 뺀 뒤 1~600자(글자 수)이고, 뺀 값을 저장합니다 (C25).
      - 형식 오류는 422 VALIDATION_ERROR 입니다(명세는 400, 현재 구현은 422. D3).
"""

OPINION_MAX_LENGTH = 600


class MemberDraftCreateRequest(CamelModel):
    response_option_id: uuid.UUID | None = Field(
        default=None,
        description="고른 대응안. 같은 실행의 대응안이어야 하며, 실행 가능 여부(FEASIBLE·NEEDS_CHECK·NOT_FEASIBLE)는 묻지 않습니다(C24)",
    )
    opinion_text: str | None = Field(
        default=None,
        description=f"의견. 앞뒤 공백을 뺀 뒤 1~{OPINION_MAX_LENGTH}자 (C25)",
        examples=[
            "항공 전환 비용이 커서 출항 전 분할 선적을 먼저 검토하면 좋겠습니다."
        ],
    )

    @field_validator("opinion_text")
    @classmethod
    def strip_opinion(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("의견은 공백만으로 쓸 수 없습니다.")
        if len(stripped) > OPINION_MAX_LENGTH:
            raise ValueError(f"의견은 {OPINION_MAX_LENGTH}자 이하여야 합니다.")
        return stripped

    @model_validator(mode="after")
    def require_option_or_opinion(self) -> MemberDraftCreateRequest:
        if self.response_option_id is None and self.opinion_text is None:
            raise ValueError(
                "대응안(responseOptionId)과 의견(opinionText) 중 하나는 있어야 합니다."
            )
        return self


class MemberDraftCreateResponse(CamelModel):
    member_draft_id: uuid.UUID
    created_at: datetime = Field(
        description="작성 시각 (UTC, 판단 시각). 데모(가상 시각)에서는 고객사의 가장 최근 수집 시작 시각이라 같은 날의 초안·최종 선택이 같은 값일 수 있습니다. 같은 값이면 ID(UUIDv7) 순이 작성 순입니다"
    )
