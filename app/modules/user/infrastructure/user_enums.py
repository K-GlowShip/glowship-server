from enum import Enum


class UserRoleCode(str, Enum):
    """사용자 권한 코드."""

    OWNER = "OWNER"
    ADMIN = "ADMIN"
    CLIENT_MEMBER = "CLIENT_MEMBER"
    CLIENT_LEAD = "CLIENT_LEAD"


# 고객사에 소속되는 역할 (users_ck2). Admin 계정 관리 API 가 다루는 계정의 범위다.
CLIENT_ROLE_CODES = (UserRoleCode.CLIENT_MEMBER, UserRoleCode.CLIENT_LEAD)


class UserStatus(str, Enum):
    """사용자 계정 상태."""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
