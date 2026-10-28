from app.core.security.hashing import hash_password
from app.modules.user.application.exception.user_exceptions import (
    EmailDuplicatedException,
)
from app.modules.user.application.password import (
    decode_password,
    validate_password_policy,
)
from app.modules.user.infrastructure.repository.user_repository import UserRepository


class UserService:
    def __init__(self, user_repo: UserRepository):
        self.user_repo = user_repo

    async def create_user(self, email: str, encoded_password: str) -> None:
        """
        새로운 회원을 가입시키는 비즈니스 로직입니다.

        Args:
            email (str): 가입할 이메일 주소
            encoded_password (str): 프론트엔드에서 btoa()로 인코딩하여 보낸 Base64 비밀번호 문자열
        """
        if await self.user_repo.find_by_email(email):
            raise EmailDuplicatedException()

        raw_password = decode_password(encoded_password)
        validate_password_policy(raw_password)

        await self.user_repo.create_admin_user(
            email=email,
            password_hash=hash_password(raw_password),
        )
