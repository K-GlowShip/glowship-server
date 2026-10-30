import uuid

from sqlalchemy import UUID, bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.user.infrastructure.user import User
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus

# 고객사 사용자 생성은 DB 함수(초기 마이그레이션 _create_rls)로만 가능하다.
# 운영용 앱 DB 역할은 users INSERT 권한이 없고, 이 SECURITY DEFINER 함수가 대신 INSERT 한다.
# 함수는 app.user_id(요청자)·app.company_id(대상 고객사) 컨텍스트를 읽어 배정된 ADMIN 인지 검사한다.
# 컨텍스트는 호출 측(서비스)이 요청 시작 시 set_rls_context 로 같은 트랜잭션에 설정해 둔다.
_CREATE_CLIENT_USER = text(
    "SELECT app_admin_create_client_user("
    ":user_id, :email, :password_hash, :display_name, :role_code)"
).bindparams(bindparam("user_id", type_=UUID(as_uuid=True)))


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

    async def create_client_user(
        self,
        *,
        user_id: uuid.UUID,
        email: str,
        password_hash: str,
        display_name: str,
        role_code: UserRoleCode,
    ) -> User:
        """
        고객사 사용자 저장. ORM INSERT 가 아니라 DB 함수 app_admin_create_client_user 를 호출한다.

        같은 트랜잭션에 RLS 컨텍스트(app.user_id·app.company_id)가 설정되어 있어야 함수가 요청자·고객사를 안다.
        함수가 권한 검사에 실패하면 insufficient_privilege(42501), 이메일 중복이면 unique_violation(23505)
        오류를 던지며, HTTP 변환은 서비스 계층이 담당한다.
        """
        await self.db.execute(
            _CREATE_CLIENT_USER,
            {
                "user_id": user_id,
                "email": email,
                "password_hash": password_hash,
                "display_name": display_name,
                "role_code": role_code.value,
            },
        )
        # 함수는 user_id 만 돌려주므로 created_at 등 DB 기본값을 포함해 다시 읽는다.
        return await self.find_by_id(user_id)

    # Read
    async def find_by_email(self, email: str) -> User | None:
        """이메일로 유저 조회"""
        stmt = select(User).where(User.email == email)
        return await self.db.scalar(stmt)

    async def find_by_id(self, user_id: uuid.UUID) -> User | None:
        """식별자로 유저 조회"""
        stmt = select(User).where(User.user_id == user_id)
        return await self.db.scalar(stmt)
