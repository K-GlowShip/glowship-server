from enum import Enum


class CompanyStatus(str, Enum):
    """고객사 상태. companies.status CHECK 제약(companies_ck1)과 같은 값이다."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
