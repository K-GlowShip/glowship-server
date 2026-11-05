from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.inventory.application.inventory_list_service import (
    InventoryListService,
)
from app.modules.inventory.application.inventory_status_service import (
    InventoryStatusService,
)
from app.modules.inventory.infrastructure.repository.inventory_repository import (
    InventoryRepository,
)
from app.modules.inventory.infrastructure.repository.inventory_status_repository import (
    InventoryStatusRepository,
)

"""
    inventory 모듈 의존성 조립
      - 요청 단위 DB 세션(get_db)을 받아 리포지토리와 서비스를 만들어 라우터에 주입합니다.
      - 같은 요청의 get_tenant_context 가 이 세션에 RLS 컨텍스트를 설정해 둡니다 (FastAPI 의존성 캐시).
"""


def get_inventory_repository(
    db: AsyncSession = Depends(get_db),
) -> InventoryRepository:
    return InventoryRepository(db)


def get_inventory_list_service(
    repo: InventoryRepository = Depends(get_inventory_repository),
) -> InventoryListService:
    return InventoryListService(repo=repo)


def get_inventory_status_service(
    db: AsyncSession = Depends(get_db),
) -> InventoryStatusService:
    return InventoryStatusService(repo=InventoryStatusRepository(db))
