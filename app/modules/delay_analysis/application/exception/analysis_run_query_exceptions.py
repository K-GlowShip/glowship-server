from starlette import status

from app.core.exceptions import BaseErrorCode, BusinessException


class AnalysisRunQueryErrorCode(BaseErrorCode):
    """delay_analysis 모듈 분석 실행 조회 에러 코드: (status_code, code, message)"""

    # 다른 고객사의 실행도 존재 여부를 드러내지 않도록 같은 404 로 응답한다(명세 6절 {리소스}_NOT_FOUND).
    ANALYSIS_RUN_NOT_FOUND = (
        status.HTTP_404_NOT_FOUND,
        "ANALYSIS_RUN_NOT_FOUND",
        "요청한 분석 실행을 찾을 수 없습니다.",
    )


class AnalysisRunNotFoundException(BusinessException):
    def __init__(self):
        super().__init__(AnalysisRunQueryErrorCode.ANALYSIS_RUN_NOT_FOUND)
