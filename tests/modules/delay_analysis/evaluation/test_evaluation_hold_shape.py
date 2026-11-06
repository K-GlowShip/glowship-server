import asyncio
import uuid

import pytest

from app.modules.delay_analysis.application.agent.retry import hold_summary
from app.modules.delay_analysis.application.result_save.result_save_service import (
    ResultSaveInputError,
    save_analysis_failure,
)

"""
    GS-06 보류를 서버 모양으로: 엔진 Hold(REQUIRED_DATA_INVALID) → hold_summary → analysis_runs 열(GLOW-128 저장)
      - 정답표: ON_HOLD, VALIDATING / REQUIRED_DATA_INVALID, 시도 1 / 소진 false, 의존 범위 NULL, 결과 행·서명 없음.
      - 보류 요약으로 결과 행을 저장하려 하면 거부한다(0이 아니라 없음).
"""

EXPECTED_COLUMNS = {
    "status": "ON_HOLD",
    "completed_status": "ON_HOLD",
    "result_class": None,
    "dependency_scope": None,
    "failed_step": "VALIDATING",
    "failure_code": "REQUIRED_DATA_INVALID",
    "attempt_count": 1,
    "retry_exhausted": False,
}


def test_gs06_hold_columns_match_answer_sheet(glow50):
    engine = glow50.gs06()
    columns = hold_summary(engine["hold"], engine["message"]).columns()
    assert {k: columns[k] for k in EXPECTED_COLUMNS} == EXPECTED_COLUMNS
    assert columns["failure_message"] == engine["message"]
    untouched = {
        "decision_signature",
        "decision_signature_sha256",
        "explanation_validation_status",
    }
    assert not untouched & columns.keys()


def test_gs06_hold_refuses_result_rows(glow50):
    engine = glow50.gs06()
    with pytest.raises(ResultSaveInputError):
        asyncio.run(
            save_analysis_failure(
                None,  # 입력 검사에서 멈추므로 DB 세션을 쓰지 않는다
                company_id=uuid.uuid4(),
                analysis_run_id=uuid.uuid4(),
                summary=hold_summary(engine["hold"], engine["message"]),
                computed=object(),  # 계산 결과가 "있다"는 사실만 필요하다
            )
        )
