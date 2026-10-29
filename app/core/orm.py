import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar

from sqlalchemy import (
    UUID,
    Boolean,
    Date,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, mapped_column

utc_datetime_type = DateTime(timezone=True)


created_at = Annotated[
    datetime,
    mapped_column(
        utc_datetime_type,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        comment="생성 일시 (UTC)",
    ),
]

updated_at = Annotated[
    datetime,
    mapped_column(
        utc_datetime_type,
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        onupdate=func.now(),
        comment="수정 일시 (UTC)",
    ),
]

# 테이블 식별자: UUIDv7 기본값 (합성데이터 적재 시에는 CSV의 UUID를 그대로 사용)
uuid_pk = Annotated[
    uuid.UUID,
    mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid7),
]


class Base(DeclarativeBase):
    # PK 이름을 DDL(erd_glowship_postgres_v5)과 같은 "{테이블}_pk"로 맞춘다.
    # UNIQUE·CHECK·FK 이름은 DDL 번호(uk1, ck1, fk_{테이블}__{컬럼})를 따르므로 모델에서 직접 지정한다.
    metadata = MetaData(
        naming_convention={
            "pk": "%(table_name)s_pk",
            "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        }
    )

    # Mapped[파이썬 타입] → PostgreSQL 타입 기본 매핑 (DDL과 동일하게 text·timestamptz 사용)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        str: Text(),
        int: Integer(),
        bool: Boolean(),
        date: Date(),
        datetime: utc_datetime_type,
        Decimal: Numeric(),
        uuid.UUID: UUID(as_uuid=True),
        dict[str, Any]: JSONB(),
    }
