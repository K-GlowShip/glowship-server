from enum import Enum


class UserRoleCode(str, Enum):
    """사용자 권한 코드."""

    OWNER = "OWNER"
    ADMIN = "ADMIN"
    CLIENT_MEMBER = "CLIENT_MEMBER"
    CLIENT_LEAD = "CLIENT_LEAD"


class UserStatus(str, Enum):
    """사용자 계정 상태."""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
