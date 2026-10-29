from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.user.application.user_service import UserService
from app.modules.user.infrastructure.repository.user_repository import UserRepository

"""
    user 모듈 의존성 조립
      - 요청 단위 DB 세션(get_db)을 받아 리포지토리와 서비스를 만들어 라우터에 주입합니다.
"""


def get_user_repository(db: AsyncSession = Depends(get_db)) -> UserRepository:
    return UserRepository(db)


def get_user_service(
    user_repo: UserRepository = Depends(get_user_repository),
) -> UserService:
    return UserService(user_repo=user_repo)
