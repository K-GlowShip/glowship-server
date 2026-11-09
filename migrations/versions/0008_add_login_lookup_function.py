"""add login lookup function app_auth_find_login_user (SECURITY DEFINER)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-09 12:00:00

로그인은 아직 누가 로그인하는지 모르는 상태(app.user_id·app.company_id 없음)에서 이메일로 사용자를 찾는다.
앱 역할(glowship_v4_app)의 users 정책 users_select_tenant_or_self(0001)는 "같은 고객사이거나 본인"만 보여 주므로,
운영처럼 앱 역할로 연결하면 이 조회가 0행이 되어 모든 로그인이 401 이 된다(이슈 #41, 데모 DB 에서 SET ROLE 로 0행 확인).
  - 0001 의 app_admin_create_client_user 와 같은 방식: 소유자 역할이 가진 SECURITY DEFINER 함수가
    이메일 하나에 해당하는 사용자만 돌려주고, 실행 권한은 앱 역할에만 준다(PUBLIC·워커 없음).
  - 돌려주는 열은 로그인 판정에 필요한 것만(식별자·고객사·역할·비밀번호 해시·상태). 표시 이름 등은 로그인 뒤
    컨텍스트를 넣고 읽는다(GET /me).
  - 비밀번호 검증(bcrypt)은 서버가 한다. 함수는 해시를 비교하지 않는다.
  - 데이터 팀 DDL(v6.2)에는 없는 함수다. glowship-plan DDL 반영은 별도 요청.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0001·v6.2 DDL 과 같은 역할 이름
OWNER_ROLE = "glowship_v4_owner"
APP_ROLE = "glowship_v4_app"
FUNCTION = "app_auth_find_login_user(text)"

CREATE_SQL = """
CREATE FUNCTION app_auth_find_login_user(p_email text)
RETURNS TABLE (user_id uuid, company_id uuid, role_code text, password_hash text, status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $f$
  SELECT u.user_id, u.company_id, u.role_code::text, u.password_hash::text, u.status::text
    FROM users u
   WHERE u.email = p_email
$f$
"""


def upgrade() -> None:
    op.execute(CREATE_SQL)
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {OWNER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}")
