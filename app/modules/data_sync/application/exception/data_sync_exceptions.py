from starlette import status

from app.core.exceptions import BaseErrorCode, BusinessException


class DataSyncErrorCode(BaseErrorCode):
    """data_sync 모듈 에러 코드: (status_code, code, message)"""

    # API 명세 v6.2 4.3 POST /data-sync-runs: 같은 원천에 실행 중인 수집이 있으면 409
    SYNC_IN_PROGRESS = (
        status.HTTP_409_CONFLICT,
        "SYNC_IN_PROGRESS",
        "같은 원천의 수집이 이미 진행 중입니다. 끝난 뒤 다시 시도해 주십시오.",
    )

    # 수동 수집(API 25)은 지금 SHIPMENT(가상 TMS)만 원천이 있다 (2026-11-05 결정). 나머지 원천은 요청 형식 오류와 같은 422.
    SOURCE_NOT_SUPPORTED = (
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "VALIDATION_ERROR",
        "수동 수집은 아직 선적(SHIPMENT)만 지원합니다.",
    )


class SyncInProgressException(BusinessException):
    def __init__(self):
        super().__init__(DataSyncErrorCode.SYNC_IN_PROGRESS)


class SourceNotSupportedException(BusinessException):
    def __init__(self):
        super().__init__(DataSyncErrorCode.SOURCE_NOT_SUPPORTED)
