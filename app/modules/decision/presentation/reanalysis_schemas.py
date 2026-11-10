import uuid

from pydantic import Field, field_validator

from app.core.schemas import CamelModel
from app.core.text_input import clean_free_text

"""
    재분석 요청 요청·응답 (API 20 POST /analysis-runs/{runId}/reanalysis, API 명세 v6.2 4.6절)
      - 본문은 선택입니다. 본문 없이 보내도 되고, requestReason 만 보낼 수 있습니다.
      - requestReason 은 의견·선택 이유(C25)와 같은 기준입니다: 앞뒤 공백을 빼고, 비면 없는 것(null)으로 보며,
        600자(글자 수) 이하입니다. 선택 항목이라 공백만 보내도 거부하지 않고 null 로 저장합니다(PR #46 리뷰).
        NUL 문자는 거부합니다(이슈 #49, core/text_input.py).
"""

REQUEST_REASON_MAX_LENGTH = 600


class ReanalysisCreateRequest(CamelModel):
    request_reason: str | None = Field(
        default=None,
        description=(
            "재분석을 요청하는 이유 (선택, analysis_runs.request_reason). "
            f"앞뒤 공백을 빼고 비면 null, {REQUEST_REASON_MAX_LENGTH}자 이하"
        ),
        examples=["입고 예정일이 다시 바뀌어 재분석이 필요합니다."],
    )

    @field_validator("request_reason")
    @classmethod
    def strip_request_reason(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return clean_free_text(
            v,
            label="재분석 요청 이유",
            max_length=REQUEST_REASON_MAX_LENGTH,
            blank_is_none=True,
        )


class ReanalysisCreateResponse(CamelModel):
    run_id: uuid.UUID = Field(description="새로 만든 분석 실행")
    version_no: int = Field(description="새 실행 버전 (이전 버전 + 1)")
    parent_run_id: uuid.UUID = Field(description="재분석을 요청한 이전 실행")
    status: str = Field(
        description="항상 DETECTED (분석은 입력 확정부터 다시 시작)",
        examples=["DETECTED"],
    )
