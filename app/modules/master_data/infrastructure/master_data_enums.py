from enum import Enum


class PopularityTier(str, Enum):
    """상품 등급. skus.popularity_tier CHECK 제약(skus_ck1)과 같은 값이다. 화면의 "히트 상품"은 HIT 다."""

    HIT = "HIT"
    REGULAR = "REGULAR"
    LONG_TAIL = "LONG_TAIL"


class SkuStatus(str, Enum):
    """상품 상태. skus.status CHECK 제약(skus_ck3)과 같은 값이다."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class MappingStatus(str, Enum):
    """외부 코드 매핑 상태. external_entity_mappings.mapping_status CHECK 제약(ck3)과 같은 값이다."""

    PENDING = "PENDING"
    MAPPED = "MAPPED"
    UNMAPPED = "UNMAPPED"
