import uuid

from pydantic import Field

from app.core.schemas import CamelModel

"""
    재분석 요청 요청·응답 (API 20 POST /analysis-runs/{runId}/reanalysis, API 명세 v6.2 4.6절)
      - 본문은 선택입니다. 본문 없이 보내도 되고, requestReason 만 보낼 수 있습니다.
"""


class ReanalysisCreateRequest(CamelModel):
    request_reason: str | None = Field(
        default=None,
        description="재분석을 요청하는 이유 (선택, analysis_runs.request_reason)",
        examples=["입고 예정일이 다시 바뀌어 재분석이 필요합니다."],
    )


class ReanalysisCreateResponse(CamelModel):
    run_id: uuid.UUID = Field(description="새로 만든 분석 실행")
    version_no: int = Field(description="새 실행 버전 (이전 버전 + 1)")
    parent_run_id: uuid.UUID = Field(description="재분석을 요청한 이전 실행")
    status: str = Field(
        description="항상 DETECTED (분석은 입력 확정부터 다시 시작)",
        examples=["DETECTED"],
    )
