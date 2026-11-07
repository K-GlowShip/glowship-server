"""
GS-06 필수 자료 부족 → 분석 보류 (02_시나리오와정답.md 5절 GS-06)
  - WEST × CREAM20 의 D−1(03-09) inventory_daily 행이 없으면 엔진 재생이 Hold 를 던진다.
  - 기대: ON_HOLD, VALIDATING / REQUIRED_DATA_INVALID, 시도 1 / 소진 false, 의존 범위 NULL,
    ①~⑤·대응안·서명 생성 안 함(0이 아니라 없음). 대조군(행 있음)은 보류 없이 reserved 50.
  - 서버 실패 저장 모양(save_analysis_failure)과의 대조는 test_evaluation_hold_shape.py.
"""

EXPECTED = {
    "hold": "REQUIRED_DATA_INVALID",
    "status": "ON_HOLD",
    "failed_step": "VALIDATING",
    "failure_code": "REQUIRED_DATA_INVALID",
    "attempt_count": 1,
    "retry_exhausted": False,
    "dependency_scope": None,
    "analysis_outputs": None,
    "control_without_gap": "no hold, reserved 50",
}


def test_hold_matches_answer_sheet(glow50):
    got = glow50.gs06()
    assert {k: got.get(k) for k in EXPECTED} == EXPECTED
    assert got["message"]  # 보류 메시지가 있다(내용은 엔진 몫)
