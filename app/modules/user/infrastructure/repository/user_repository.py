from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.user.infrastructure.user import User
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus


class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # Create
    async def create_admin_user(self, email: str, password_hash: str) -> User:
        """Glowship내부 운영 관리자 유저 저장"""
        user = User(
            email=email,
            password_hash=password_hash,
            role_code=UserRoleCode.ADMIN,
            status=UserStatus.ACTIVE,
        )
        self.db.add(user)
        await self.db.flush()  # user_id 등 기본값 생성 및 제약 조건 즉시 검증
        return user

    # Read
    async def find_by_email(self, email: str) -> User | None:
        """이메일로 유저 조회"""
        stmt = select(User).where(User.email == email)
        return await self.db.scalar(stmt)
