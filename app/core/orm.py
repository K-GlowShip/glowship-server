from datetime import UTC, datetime
from typing import Annotated

from sqlalchemy import DateTime, func
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


class Base(DeclarativeBase):
    pass
