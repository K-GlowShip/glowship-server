from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.order.application.order_list_service import OrderListService
from app.modules.order.infrastructure.repository.order_repository import (
    OrderRepository,
)

"""
    order 모듈 의존성 조립
      - 요청 단위 DB 세션(get_db)을 받아 리포지토리와 서비스를 만들어 라우터에 주입합니다.
      - 같은 요청의 get_tenant_context 가 이 세션에 RLS 컨텍스트를 설정해 둡니다 (FastAPI 의존성 캐시).
"""


def get_order_repository(db: AsyncSession = Depends(get_db)) -> OrderRepository:
    return OrderRepository(db)


def get_order_list_service(
    repo: OrderRepository = Depends(get_order_repository),
) -> OrderListService:
    return OrderListService(repo=repo)
