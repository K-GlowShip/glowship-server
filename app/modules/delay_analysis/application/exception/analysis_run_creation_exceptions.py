from enum import StrEnum

"""
    분석 실행 생성(create_analysis_run) 실패 사유
      - 이 함수는 지연 감지(수집 적재)와 재분석 API 가 부르는 내부 함수라 HTTP 상태를 정하지 않는다.
        재분석 API(B) 는 code 를 보고 응답 코드(예: 지연 없음 → 404)를 정한다.
      - 실패하면 DB 를 바꾸지 않은 상태에서 올린다(이전 실행의 is_latest 도 그대로).
"""


class RunCreationErrorCode(StrEnum):
    INVALID_TRIGGER = "INVALID_TRIGGER"  # 사유와 인자 조합이 DDL 규칙(ck2·ck3·ck4·ck15~ck17)과 맞지 않음
    DELAY_EVENT_NOT_FOUND = "DELAY_EVENT_NOT_FOUND"  # 같은 고객사에 그 지연이 없음
    SNAPSHOT_NOT_OF_SHIPMENT = (
        "SNAPSHOT_NOT_OF_SHIPMENT"  # 트리거 스냅샷이 지연 선적의 것이 아님
    )
    EPISODE_START_MISMATCH = (
        "EPISODE_START_MISMATCH"  # 첫 실행 ⇔ EPISODE_STARTED 규칙(ck18) 위반
    )
    OPERATING_RULE_MISSING = (
        "OPERATING_RULE_MISSING"  # 기준일에 적용 중인 운영 기준이 없음
    )
    RUN_CODE_EXHAUSTED = "RUN_CODE_EXHAUSTED"  # 그날 run_code 순번 999 를 다 씀
    # 넘겨받은 실행 시각이 고객사 마지막 적재 수집 시작보다 이름 → 엔진 입력 순번과 어긋남(이슈 #50)
    RUN_TIME_BEFORE_INPUT = "RUN_TIME_BEFORE_INPUT"


class AnalysisRunCreationError(Exception):
    def __init__(self, code: RunCreationErrorCode, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
