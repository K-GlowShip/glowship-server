import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import jwt


class TokenError(Exception):
    """서명 불일치·만료·용도 불일치 등 토큰을 신뢰할 수 없을 때. HTTP 변환은 호출 측(인증 의존성)이 담당한다."""


class TokenType(str, Enum):
    """
    토큰 용도. 페이로드의 `type` 클레임에 기록해 access 토큰을 refresh 용도로 쓰는 것을 막는다.
    쿠키 이름(access_token / refresh_token)과 구분하기 위해 하이픈 표기를 쓴다.
    """

    ACCESS = "access-token"
    REFRESH = "refresh-token"


@dataclass(frozen=True)
class JwtConfig:
    """토큰 서명·만료 설정. 환경 설정(Settings)에서 만들어 발급 함수에 넘긴다."""

    secret_key: str
    algorithm: str
    access_token_expires: timedelta
    refresh_token_expires: timedelta


def _create_token(
    *,
    subject: uuid.UUID,
    token_type: TokenType,
    expires_in: timedelta,
    config: JwtConfig,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """
    공통 클레임을 채워 서명된 JWT 문자열을 만든다.

    RFC 7519 등록 클레임
    - sub: 사용자 식별자 (PyJWT 는 sub 가 문자열이어야 하므로 UUID 를 str 로 변환)
    - iat / exp: 발급·만료 시각 (PyJWT 가 datetime 을 UNIX timestamp 로 변환)
    - jti: 토큰 고유 식별자 (이후 폐기·회전 구현 시 식별 키로 사용)

    비공개 클레임 (RFC 7519 4.3)
    - type: 토큰 용도 (access-token / refresh-token)
    - extra_claims: 토큰 종류별 추가 클레임
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type.value,
        "iat": now,
        "exp": now + expires_in,
        "jti": uuid.uuid7().hex,
        **(extra_claims or {}),
    }
    return jwt.encode(payload, config.secret_key, algorithm=config.algorithm)


def create_access_token(
    user_id: uuid.UUID,
    role_code: str,
    company_id: uuid.UUID | None,
    config: JwtConfig,
) -> str:
    """
    API 접근용 access 토큰 발급.

    매 요청에서 DB 조회 없이 권한·테넌트를 판단할 수 있도록 role 과 company_id 를 담는다.
    OWNER·ADMIN 은 소속 회사가 없으므로 company_id 를 생략하지 않고 null 로 넣어
    "클라이언트 사용자가 아님" 을 명시한다.
    """
    return _create_token(
        subject=user_id,
        token_type=TokenType.ACCESS,
        expires_in=config.access_token_expires,
        config=config,
        extra_claims={
            "role": role_code,
            "company_id": str(company_id) if company_id is not None else None,
        },
    )


def create_refresh_token(
    user_id: uuid.UUID,
    config: JwtConfig,
) -> str:
    """
    access 토큰 재발급용 refresh 토큰 발급.

    재발급 시점에 DB 에서 사용자를 다시 읽어 현재 권한으로 access 토큰을 만들므로
    role·company_id 는 담지 않고 신원 증명에 필요한 최소 클레임만 둔다.
    """
    return _create_token(
        subject=user_id,
        token_type=TokenType.REFRESH,
        expires_in=config.refresh_token_expires,
        config=config,
    )


# 발급 시 항상 넣는 클레임. 하나라도 빠진 토큰은 우리가 발급한 것이 아니므로 거절한다.
_REQUIRED_CLAIMS = ["sub", "type", "iat", "exp", "jti"]


def decode_token(
    token: str,
    config: JwtConfig,
    expected_type: TokenType,
) -> dict[str, Any]:
    """
    토큰의 서명·만료·필수 클레임을 검증하고 페이로드를 돌려준다.

    Args:
        token: JWT 문자열
        config: 발급 때와 같은 서명 설정
        expected_type: 기대하는 용도. access 토큰으로 재발급하거나 refresh 토큰으로 API 를 호출하는 것을 막는다.

    Raises:
        TokenError: 서명 불일치, 만료, 필수 클레임 누락, 용도 불일치
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            config.secret_key,
            algorithms=[config.algorithm],
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.InvalidTokenError as e:
        raise TokenError(str(e)) from e

    if payload["type"] != expected_type.value:
        raise TokenError(f"token type mismatch: expected {expected_type.value}")

    return payload
