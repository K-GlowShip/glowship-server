"""add analysis_runs.data_warnings (data warnings decided when analysis starts, issue #122)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-12 16:00:00

명세 v6.2 4.5 "입력 기준"의 dataWarnings[] 를 저장할 열을 analysis_runs.data_warnings 로 추가한다(이슈 #122).
v6.2 DDL 에는 이 열이 없고, 서버 리비전 0010 으로 넣는다(0006 run_code 와 같은 방식, DDL·ERD 반영은 설계 담당에 요청).
  - 값: 분석을 시작할 때(엔진 입력을 만들 때) 정한 원천별 경고 목록(JSON 배열). 결과 저장과 같은 트랜잭션에서 쓴다.
    수집 실패(마지막으로 끝난 수집이 FAILED)·기준일 D 를 당김(재고·주문 자료가 실행 날짜까지 없음).
  - NULL = 정하지 않음(분석 전, 지연 해소, 그래프 밖 실패, 이 열이 생기기 전 실행). [] = 정했는데 없음. 응답은 둘 다 빈 목록.
  - CHECK(analysis_runs_data_warnings_ck)는 모양(배열)만 고정한다. 상태와 묶지 않는다: 값이 여러 완료 상태에 있고,
    정답 실행을 DETECTED 로 되돌리는 평가 장치가 정해진 열만 비운다.
  - 기존 행은 채우지 않는다(NULL). 값을 정하려면 실행마다 엔진 저장소를 읽어야 해 마이그레이션에서 할 수 없다.
  - 워커 역할 권한은 0001 의 표 단위 GRANT(SELECT·INSERT·UPDATE ON analysis_runs)가 새 열에도 미쳐 더하지 않는다.
  - 같은 열·제약이 이미 있으면 아무것도 바꾸지 않는다. 열만 있고 제약이 없으면 멈춘다(0006 과 같음).
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

DATA_WARNINGS_CK = "analysis_runs_data_warnings_ck"
DATA_WARNINGS_CK_SQL = "data_warnings IS NULL OR jsonb_typeof(data_warnings) = 'array'"


def _existing_shape(bind) -> tuple[bool, bool]:
    has_column = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'analysis_runs'"
            " AND column_name = 'data_warnings')"
        )
    ).scalar()
    has_check = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM pg_constraint"
            " WHERE conrelid = 'public.analysis_runs'::regclass AND conname = :ck)"
        ),
        {"ck": DATA_WARNINGS_CK},
    ).scalar()
    return bool(has_column), bool(has_check)


def upgrade() -> None:
    bind = op.get_bind()
    has_column, has_check = _existing_shape(bind)
    if has_column:
        if has_check:
            logger.info(
                "0010: analysis_runs.data_warnings 와 제약이 이미 있어 아무것도 바꾸지 않습니다"
            )
            return
        raise RuntimeError(
            "0010 중단: analysis_runs.data_warnings 열은 있는데 제약"
            f" {DATA_WARNINGS_CK} 이 없습니다. 어떤 판의 DDL 로 만든 DB 인지 확인하세요."
            " 아무것도 바꾸지 않았습니다."
        )
    op.add_column(
        "analysis_runs",
        sa.Column("data_warnings", postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(DATA_WARNINGS_CK, "analysis_runs", DATA_WARNINGS_CK_SQL)


def downgrade() -> None:
    # 분석을 시작할 때 정한 값이라 다시 만들 수 없다. 내리면 경고 기록이 사라진다(응답은 빈 목록으로 돌아감)
    op.drop_constraint(DATA_WARNINGS_CK, "analysis_runs", type_="check")
    op.drop_column("analysis_runs", "data_warnings")
