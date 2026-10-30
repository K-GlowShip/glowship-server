"""widen users.display_name to varchar(50)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-30 14:10:00

display_name 을 생략하면 email 을 그대로 쓰는데, email 은 varchar(50)·display_name 은 varchar(30) 이라
31자 이상 이메일에서 길이 초과 오류(500)가 났다. 두 길이를 50 으로 맞춘다.
  - downgrade 는 30자를 넘는 display_name 행이 있으면 실패한다 (데이터를 먼저 줄여야 함)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "display_name",
        existing_type=sa.String(length=30),
        type_=sa.String(length=50),
        existing_nullable=False,
        existing_comment="회원 표시 이름",
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "display_name",
        existing_type=sa.String(length=50),
        type_=sa.String(length=30),
        existing_nullable=False,
        existing_comment="회원 표시 이름",
    )
