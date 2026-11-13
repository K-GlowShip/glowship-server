import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UnauthenticatedException
from app.core.rls_context import set_rls_context
from app.core.security.hashing import (
    BCRYPT_MAX_PASSWORD_BYTES,
    DUMMY_PASSWORD_HASH,
    hash_password,
    verify_password,
)
from app.core.security.jwt import (
    JwtConfig,
    TokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.modules.user.application.exception.user_exceptions import (
    AccountDisabledException,
    EmailDuplicatedException,
    InvalidCredentialsException,
)
from app.modules.user.application.password import (
    decode_password,
    validate_password_policy,
)
from app.modules.user.infrastructure.repository.user_repository import UserRepository
from app.modules.user.infrastructure.user_enums import UserRoleCode, UserStatus


@dataclass(frozen=True)
class TokenPair:
    """로그인 성공 시 발급되는 토큰 묶음. 쿠키에 싣는 일은 presentation 계층이 담당한다."""

    access_token: str
    refresh_token: str


class AuthService:
    def __init__(
        self, db: AsyncSession, user_repo: UserRepository, jwt_config: JwtConfig
    ):
        # 리포지토리와 같은 요청 단위 세션. 재발급의 RLS 컨텍스트 설정에만 사용한다.
        self.db = db
        self.user_repo = user_repo
        self.jwt_config = jwt_config

    async def create_user(self, email: str, encoded_password: str) -> None:
        """
        새로운 회원을 가입시키는 비즈니스 로직입니다.

        Args:
            email (str): 가입할 이메일 주소
            encoded_password (str): 프론트엔드에서 btoa()로 인코딩하여 보낸 Base64 비밀번호 문자열
        """
        # DB 에 접근하지 않는 검사를 먼저 수행한다.
        # 잘못된 입력마다 DB 를 조회하지 않고, 비밀번호 없이 이메일 가입 여부를 알아내는 것도 막는다.
        raw_password = decode_password(encoded_password)
        validate_password_policy(raw_password)

        if await self.user_repo.find_by_email(email):
            raise EmailDuplicatedException()

        password_hash = await hash_password(raw_password)

        try:
            await self.user_repo.create_admin_user(
                email=email,
                password_hash=password_hash,
            )
        except IntegrityError as e:
            raise EmailDuplicatedException() from e

    async def login(self, email: str, encoded_password: str) -> TokenPair:
        """
        이메일·비밀번호를 확인하고 access / refresh 토큰을 발급합니다.

        Args:
            email (str): 로그인 이메일
            encoded_password (str): 프론트엔드에서 btoa()로 인코딩하여 보낸 Base64 비밀번호 문자열

        Raises:
            InvalidPasswordFormatException: Base64 디코딩 실패 (400)
            InvalidCredentialsException: 이메일 없음 또는 비밀번호 불일치 (401, 두 경우를 구분하지 않음)
            AccountDisabledException: 비활성화된 계정 (403)
        """
        raw_password = decode_password(encoded_password)

        # 정책상 비밀번호는 16자 이하이므로 bcrypt 한도를 넘는 입력은 조회 없이 거절한다. (ValueError → 500 방지)
        if len(raw_password.encode("utf-8")) > BCRYPT_MAX_PASSWORD_BYTES:
            raise InvalidCredentialsException()

        # 컨텍스트 없이 하는 조회라 DB 함수로 찾는다(앱 DB 역할에서 select(User) 는 0행, 이슈 #41).
        user = await self.user_repo.find_login_user(email)

        # 계정이 없어도 더미 해시로 검증을 수행해 응답 시간으로 계정 유무를 추측할 수 없게 한다.
        password_hash = user.password_hash if user else DUMMY_PASSWORD_HASH
        matched = await verify_password(raw_password, password_hash)
        if user is None or not matched:
            raise InvalidCredentialsException()

        if user.status != UserStatus.ACTIVE:
            raise AccountDisabledException()

        return TokenPair(
            access_token=create_access_token(
                user_id=user.user_id,
                role_code=user.role_code.value,
                company_id=user.company_id,
                config=self.jwt_config,
            ),
            refresh_token=create_refresh_token(
                user_id=user.user_id,
                config=self.jwt_config,
            ),
        )

    async def refresh_access_token(self, refresh_token: str) -> str:
        """
        refresh 토큰을 검증하고 DB 의 현재 권한으로 access 토큰 하나를 새로 발급합니다 (API 27, GLOW-152).

        refresh 토큰에는 role·company_id 가 없으므로 사용자를 DB 에서 다시 읽는다. 발급 뒤 역할·고객사가
        바뀌었으면 새 access 토큰에는 바뀐 값이 들어간다. refresh 토큰은 회전하지 않는다(쿠키 수명 고정, D4).

        Args:
            refresh_token (str): refresh_token 쿠키 값

        Raises:
            UnauthenticatedException: 서명 불일치·만료·access 토큰을 보냄·클레임 구성 불일치, 또는 토큰의 사용자가
                DB 에 없음 (401, 사유를 구분하지 않음. 프론트는 로그인 화면으로 이동)
            AccountDisabledException: 계정이 DISABLED (403). 401 과 구분해 비활성 계정이 재발급을 반복하지 않게 한다
        """
        try:
            payload = decode_token(refresh_token, self.jwt_config, TokenType.REFRESH)
            user_id = uuid.UUID(payload["sub"])
        except (TokenError, KeyError, ValueError, TypeError) as e:
            raise UnauthenticatedException() from e

        # 운영 앱 DB 역할에서는 users 의 self 정책(users_select_tenant_or_self 의 user_id 조건)이 app.user_id 로
        # 자기 행을 보여 준다. 고객사 컨텍스트는 필요 없으므로 비운다. (로컬 superuser 는 정책을 우회)
        await set_rls_context(self.db, user_id=user_id, company_id=None)

        user = await self.user_repo.find_by_id(user_id)
        if user is None:
            raise UnauthenticatedException()
        if user.status != UserStatus.ACTIVE:
            raise AccountDisabledException()

        return create_access_token(
            user_id=user.user_id,
            role_code=UserRoleCode(user.role_code).value,
            company_id=user.company_id,
            config=self.jwt_config,
        )
