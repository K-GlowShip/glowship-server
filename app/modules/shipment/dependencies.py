from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.shipment.application.shipment_list_service import (
    ShipmentListService,
)
from app.modules.shipment.infrastructure.repository.shipment_repository import (
    ShipmentRepository,
)

"""
    shipment 모듈 의존성 조립
      - 요청 단위 DB 세션(get_db)을 받아 리포지토리와 서비스를 만들어 라우터에 주입합니다.
      - 같은 요청의 get_tenant_context 가 이 세션에 RLS 컨텍스트를 설정해 둡니다 (FastAPI 의존성 캐시).
"""


def get_shipment_repository(db: AsyncSession = Depends(get_db)) -> ShipmentRepository:
    return ShipmentRepository(db)


def get_shipment_list_service(
    repo: ShipmentRepository = Depends(get_shipment_repository),
) -> ShipmentListService:
    return ShipmentListService(repo=repo)
