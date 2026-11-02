"""add users created_at and updated_at if missing (v6.2 DDL DB)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02 14:00:00

0001 은 users 에 created_at·updated_at 을 DDL 보다 더 만들었다. v6.2 DDL 로 바로 적재한 DB 에는 이 두 열이 없어
User 모델이 users 를 읽을 때(로그인 등) "column users.created_at does not exist" 로 500 이 난다.
  - 열이 없을 때만 0001 과 같은 정의(timestamptz, NOT NULL, DEFAULT now(), 주석)로 추가한다.
    v5 구조 DB(0001 로 만든 DB)는 이미 열이 있어 아무것도 바뀌지 않는다.
  - 기존 행의 두 열은 upgrade 시각으로 채워진다 (원래 생성 시각은 v6.2 데이터에 없음).
  - v6.2 DDL DB 는 stamp 0003 → upgrade 0004 순서로 이 마이그레이션을 실제로 실행해야 한다 (0003 docstring 참고).
  - downgrade 는 아무것도 하지 않는다. 두 열은 0001 이 만든 DB 에도 있으므로 여기서 지우면 그 DB 가 깨진다.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users"
        " ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now(),"
        " ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()"
    )
    op.execute("COMMENT ON COLUMN users.created_at IS '생성 일시 (UTC)'")
    op.execute("COMMENT ON COLUMN users.updated_at IS '수정 일시 (UTC)'")


def downgrade() -> None:
    pass
