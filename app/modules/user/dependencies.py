from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.modules.company.dependencies import (
    get_admin_assignment_repository,
    get_company_repository,
)
from app.modules.company.infrastructure.repository.admin_assignment_repository import (
    AdminAssignmentRepository,
)
from app.modules.company.infrastructure.repository.company_repository import (
    CompanyRepository,
)
from app.modules.user.application.auth_service import AuthService
from app.modules.user.application.client_account_service import ClientAccountService
from app.modules.user.infrastructure.repository.user_repository import UserRepository

"""
    user 모듈 의존성 조립
      - 요청 단위 DB 세션(get_db)을 받아 리포지토리와 서비스를 만들어 라우터에 주입합니다.
      - get_db 는 요청마다 하나의 세션만 만들므로(FastAPI 의존성 캐시) 여러 리포지토리가 같은 트랜잭션을 공유합니다.
"""


def get_user_repository(db: AsyncSession = Depends(get_db)) -> UserRepository:
    return UserRepository(db)


def get_auth_service(
    user_repo: UserRepository = Depends(get_user_repository),
) -> AuthService:
    return AuthService(user_repo=user_repo, jwt_config=get_settings().jwt_config)


def get_client_account_service(
    db: AsyncSession = Depends(get_db),
    user_repo: UserRepository = Depends(get_user_repository),
    company_repo: CompanyRepository = Depends(get_company_repository),
    admin_assignment_repo: AdminAssignmentRepository = Depends(
        get_admin_assignment_repository
    ),
) -> ClientAccountService:
    return ClientAccountService(
        db=db,
        user_repo=user_repo,
        company_repo=company_repo,
        admin_assignment_repo=admin_assignment_repo,
    )
