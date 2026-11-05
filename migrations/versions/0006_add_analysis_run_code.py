"""add analysis_runs.run_code (display run ID, D14)

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-04 15:00:00

명세 v6.2 D14: 화면에 보이는 실행 ID(`AN-YYMMDD-NNN`)를 analysis_runs.run_code 로 추가한다.
v6.2 DDL 에는 이 열이 없고, 서버 Alembic 리비전 0006 으로 넣기로 정했다(개발자 3).
  - 형식: `AN-` + 기준일(started_at 의 UTC 날짜, serviceflow "D = 입력 확정일") `YYMMDD` + `-` + 고객사·날짜별 3자리 순번.
  - 기존 행(overlay 실행 21건 등)은 고객사·날짜별로 started_at, analysis_run_id 순서로 001 부터 채운다.
    새 실행은 분석 실행 생성(create_analysis_run)이 고객사 잠금(L1) 안에서 같은 규칙으로 다음 순번을 붙인다.
    새 실행은 생성 순서대로 started_at 이 늘고, 같은 시각이면 UUIDv7 실행 ID 가 생성 순서라 downgrade 후
    다시 upgrade 해도 보통 같은 값이 만들어진다(시각을 손으로 넣은 행은 다를 수 있음).
  - 고객사 안에서 고유(analysis_runs_run_code_uk), 형식 CHECK(analysis_runs_run_code_ck), NOT NULL.
  - 한 고객사·한 날짜에 실행이 999건을 넘으면 형식에 담을 수 없으므로 아무것도 바꾸지 않고 멈춘다.
  - 데이터 팀 DDL 새 판이 같은 열과 제약을 이미 가진 DB 는 아무것도 바꾸지 않는다. 열만 있고 제약이 다르면 멈춘다.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

RUN_CODE_CK = "analysis_runs_run_code_ck"
RUN_CODE_UK = "analysis_runs_run_code_uk"
RUN_CODE_PATTERN = "^AN-[0-9]{6}-[0-9]{3}$"
MAX_DAILY_SEQ = 999

# 고객사·기준일(UTC 날짜)별 순번. 분석 실행 생성 코드(run_code.py)와 같은 규칙이다.
_NUMBERED = """
    SELECT analysis_run_id,
           (started_at AT TIME ZONE 'UTC')::date AS basis_date,
           row_number() OVER (
               PARTITION BY company_id, (started_at AT TIME ZONE 'UTC')::date
               ORDER BY started_at, analysis_run_id
           ) AS seq
      FROM analysis_runs
"""


def _existing_shape(bind) -> tuple[bool, set[str]]:
    has_column = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = 'analysis_runs'"
            " AND column_name = 'run_code')"
        )
    ).scalar()
    constraints = set(
        bind.execute(
            sa.text(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = 'public.analysis_runs'::regclass AND conname IN (:ck, :uk)"
            ),
            {"ck": RUN_CODE_CK, "uk": RUN_CODE_UK},
        ).scalars()
    )
    return bool(has_column), constraints


def upgrade() -> None:
    bind = op.get_bind()
    has_column, constraints = _existing_shape(bind)
    if has_column:
        if constraints == {RUN_CODE_CK, RUN_CODE_UK}:
            logger.info(
                "0006: analysis_runs.run_code 와 제약이 이미 있어 아무것도 바꾸지 않습니다"
            )
            return
        raise RuntimeError(
            "0006 중단: analysis_runs.run_code 열은 있는데 제약"
            f" {sorted({RUN_CODE_CK, RUN_CODE_UK} - constraints)} 이 없습니다."
            " 어떤 판의 DDL 로 만든 DB 인지 확인하세요. 아무것도 바꾸지 않았습니다."
        )

    max_seq = bind.execute(
        sa.text(f"SELECT COALESCE(MAX(seq), 0) FROM ({_NUMBERED}) n")
    ).scalar()
    if max_seq > MAX_DAILY_SEQ:
        raise RuntimeError(
            f"0006 중단: 한 고객사·한 날짜의 실행이 {max_seq}건이라 3자리 순번(최대 {MAX_DAILY_SEQ})에"
            " 담을 수 없습니다. 아무것도 바꾸지 않았습니다."
        )

    op.add_column("analysis_runs", sa.Column("run_code", sa.Text(), nullable=True))
    op.execute(
        "UPDATE analysis_runs r"
        "   SET run_code = 'AN-' || to_char(n.basis_date, 'YYMMDD') || '-' || lpad(n.seq::text, 3, '0')"
        f"  FROM ({_NUMBERED}) n"
        " WHERE r.analysis_run_id = n.analysis_run_id"
    )
    op.alter_column("analysis_runs", "run_code", nullable=False)
    op.create_check_constraint(
        RUN_CODE_CK, "analysis_runs", f"run_code ~ '{RUN_CODE_PATTERN}'"
    )
    op.create_unique_constraint(
        RUN_CODE_UK, "analysis_runs", ["company_id", "run_code"]
    )


def downgrade() -> None:
    # 열은 기준일·순번 규칙으로 다시 만들 수 있는 표시용 값이라 따로 보호하지 않는다
    op.drop_constraint(RUN_CODE_UK, "analysis_runs", type_="unique")
    op.drop_constraint(RUN_CODE_CK, "analysis_runs", type_="check")
    op.drop_column("analysis_runs", "run_code")
