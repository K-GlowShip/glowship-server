from starlette import status

from app.core.exceptions import BaseErrorCode, BusinessException


class ShipmentErrorCode(BaseErrorCode):
    """shipment 모듈 에러 코드: (status_code, code, message)"""

    # 다른 고객사의 선적도 존재 여부를 드러내지 않도록 같은 404 로 응답한다 (명세 5절 `{리소스}_NOT_FOUND`, 지연 상세와 같음).
    SHIPMENT_NOT_FOUND = (
        status.HTTP_404_NOT_FOUND,
        "SHIPMENT_NOT_FOUND",
        "요청한 선적을 찾을 수 없습니다.",
    )


class ShipmentNotFoundException(BusinessException):
    def __init__(self):
        super().__init__(ShipmentErrorCode.SHIPMENT_NOT_FOUND)
