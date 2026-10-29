import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from fastapi import Depends
from fastapi.security import APIKeyCookie

from app.core.config import Settings, get_settings
from app.core.exceptions import ForbiddenException, UnauthenticatedException
from app.core.security.cookies import ACCESS_TOKEN_COOKIE
from app.core.security.jwt import TokenError, TokenType, decode_token

"""
    로그인 사용자 인증 의존성

    FastAPI 는 데코레이터 대신 Depends 로 인증을 건다.
      - 사용자 정보가 필요할 때: current_user: CurrentUser = Depends(get_current_user)
      - 로그인 여부만 볼 때:     @router.get(..., dependencies=[Depends(get_current_user)])
      - 역할 제한:               Depends(require_roles(UserRoleCode.ADMIN))
    access 토큰에 role·company_id 가 들어 있으므로 DB 를 조회하지 않는다.
"""


@dataclass(frozen=True)
class CurrentUser:
    """access 토큰에서 복원한 요청 주체. role_code 는 UserRoleCode 값과 같은 문자열이다."""

    user_id: uuid.UUID
    role_code: str
    company_id: uuid.UUID | None


# OpenAPI 에 쿠키 인증 방식을 기록한다. 쿠키가 없을 때의 응답은 공통 형식으로 직접 내므로 auto_error 는 끈다.
_access_token_cookie = APIKeyCookie(name=ACCESS_TOKEN_COOKIE, auto_error=False)


async def get_current_user(
    access_token: str | None = Depends(_access_token_cookie),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """access_token 쿠키를 검증해 CurrentUser 를 돌려준다. 실패 사유는 구분하지 않고 401 하나로 응답한다."""
    if not access_token:
        raise UnauthenticatedException()

    try:
        payload = decode_token(access_token, settings.jwt_config, TokenType.ACCESS)
        return CurrentUser(
            user_id=uuid.UUID(payload["sub"]),
            role_code=payload["role"],
            company_id=(
                uuid.UUID(payload["company_id"])
                if payload["company_id"] is not None
                else None
            ),
        )
    except (TokenError, KeyError, ValueError, TypeError) as e:
        # KeyError/ValueError/TypeError: 서명은 맞지만 클레임 구성이 다른 토큰 (예: 구버전 발급분)
        raise UnauthenticatedException() from e


def require_roles(
    *roles: str,
) -> Callable[..., Coroutine[Any, Any, CurrentUser]]:
    """
    허용 역할을 제한하는 의존성 팩토리. UserRoleCode 는 str Enum 이므로 그대로 넘길 수 있다.

        @router.get("/admin-only", dependencies=[Depends(require_roles(UserRoleCode.ADMIN))])
    """
    # str Enum 멤버는 같은 값의 문자열과 동등·동일 해시이므로 그대로 집합에 넣어 비교한다. (str() 는 쓰지 않는다)
    allowed = set(roles)

    async def dependency(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if current_user.role_code not in allowed:
            raise ForbiddenException()
        return current_user

    return dependency
