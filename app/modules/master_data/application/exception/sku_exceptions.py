from starlette import status

from app.core.exceptions import BaseErrorCode, BusinessException


class SkuErrorCode(BaseErrorCode):
    """master_data 모듈 상품 조회 에러 코드: (status_code, code, message)"""

    # 다른 고객사의 상품도 존재 여부를 드러내지 않도록 같은 404 로 응답한다(명세 6절 {리소스}_NOT_FOUND).
    SKU_NOT_FOUND = (
        status.HTTP_404_NOT_FOUND,
        "SKU_NOT_FOUND",
        "요청한 상품을 찾을 수 없습니다.",
    )


class SkuNotFoundException(BusinessException):
    def __init__(self):
        super().__init__(SkuErrorCode.SKU_NOT_FOUND)
