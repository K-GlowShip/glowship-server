import base64
import binascii
import re

from app.modules.user.application.exception.user_exceptions import (
    InvalidPasswordFormatException,
)

# 영문 대소문자와 숫자만 허용, 6자 이상 16자 이하
PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9]{6,16}$")


def decode_password(encoded_password: str) -> str:
    """프론트엔드가 Base64로 인코딩해 보낸 비밀번호를 평문으로 되돌립니다.

    전송 형식만 다루며, 비밀번호 정책 검사는 하지 않습니다.
    (로그인 등 기존 비밀번호를 확인하는 경로에서도 그대로 사용)
    """
    try:
        return base64.b64decode(encoded_password, validate=True).decode("utf-8")
    except binascii.Error, UnicodeDecodeError:
        raise InvalidPasswordFormatException()


def validate_password_policy(raw_password: str) -> None:
    """새 비밀번호를 정할 때(회원가입·비밀번호 변경)만 호출하는 정책 검사입니다."""
    if not PASSWORD_PATTERN.fullmatch(raw_password):
        raise InvalidPasswordFormatException()
