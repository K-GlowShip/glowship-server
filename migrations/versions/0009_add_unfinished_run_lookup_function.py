"""add unfinished analysis run lookup function app_worker_find_unfinished_runs (SECURITY DEFINER)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-09 15:30:00

분석 자동 시작(GLOW-128, 이슈 #51)은 서버가 시작될 때 끝나지 않은 실행(DETECTED~VALIDATING_OUTPUT)을 찾아
대기 중이던 것은 다시 시작하고 분석 도중 멈춘 것은 FAILED 로 정리한다(docs/analysis/auto-start.md).
이 정리는 특정 고객사의 일이 아니라 모든 고객사를 봐야 하는데, analysis_runs·companies 는 고객사 행 보안 대상이라
워커 역할(glowship_v4_worker)은 app.company_id 없이 한 행도 보지 못한다(0001 TENANT_TABLES, app_context_allowed).
로컬 superuser 는 정책을 건너뛰어 이 문제가 보이지 않는다(사용자 결정 11-09).
  - 0001 app_admin_create_client_user·0008 app_auth_find_login_user 와 같은 방식: 소유자 역할이 가진 SECURITY DEFINER
    함수가 필요한 열만 돌려주고, 실행 권한은 워커 역할에만 준다(PUBLIC·앱 역할 없음).
  - 돌려주는 열은 정리에 필요한 것만(고객사·실행 ID·상태). 실행을 바꾸는 일은 고객사 문맥을 넣은 평소 세션이 한다
    (RLS·L2 잠금 그대로).
  - ACTIVE 고객사의 실행만 돌려준다. 워커 문맥은 ACTIVE 고객사에서만 열리므로(app_context_allowed) 그 밖의 실행은
    찾아도 고칠 수 없다.
  - 데이터 팀 DDL(v6.2)에는 없는 함수다. glowship-plan DDL 반영은 별도 요청.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0001·v6.2 DDL 과 같은 역할 이름
OWNER_ROLE = "glowship_v4_owner"
WORKER_ROLE = "glowship_v4_worker"
FUNCTION = "app_worker_find_unfinished_runs()"

CREATE_SQL = """
CREATE FUNCTION app_worker_find_unfinished_runs()
RETURNS TABLE (company_id uuid, analysis_run_id uuid, status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $f$
  SELECT r.company_id, r.analysis_run_id, r.status::text
    FROM analysis_runs r
    JOIN companies c ON c.company_id = r.company_id AND c.status = 'ACTIVE'
   WHERE r.status IN ('DETECTED','VALIDATING','ANALYZING_IMPACT','ANALYZING_OPTIONS','VALIDATING_OUTPUT')
   ORDER BY r.company_id, r.started_at, r.analysis_run_id
$f$
"""


def upgrade() -> None:
    op.execute(CREATE_SQL)
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {OWNER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {WORKER_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}")
