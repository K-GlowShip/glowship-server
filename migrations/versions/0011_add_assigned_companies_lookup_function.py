"""add assigned companies lookup function app_me_list_assigned_companies (SECURITY DEFINER)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-16 12:00:00

GET /me 는 ADMIN·OWNER 에게 담당 고객사 목록(assignedCompanies[])을 내려준다(명세 C47, 프론트 GLOW-103).
앱 역할(glowship_v4_app)의 정책은 app.company_id 로 고른 고객사 1곳만 보여 준다(0001).
  - admin_assignments_self_company: 본인 배정 중 company_id = app.company_id 인 1건
  - companies 의 고객사 정책: app.company_id 인 고객사 1건
GET /me 는 고객사를 고르기 전에 부르므로(app.company_id 없음) select 로는 0행이고, OWNER 는 배정 행 자체가 없다.
  - 0008 app_auth_find_login_user 와 같은 방식: 소유자 역할이 가진 SECURITY DEFINER 함수가 대신 읽고,
    실행 권한은 앱 역할에만 준다(PUBLIC·워커 없음).
  - 인자를 받지 않는다. 누구의 목록인지는 app.user_id 로만 정하고 역할도 함수가 DB 행에서 판정하므로,
    호출하는 쪽이 다른 사용자의 목록이나 OWNER 의 전체 목록을 요청할 수 없다.
  - ACTIVE 인 ADMIN: 배정된 고객사와 배정 행의 can_manage_client_accounts
  - ACTIVE 인 OWNER: 전체 고객사, can_manage_client_accounts 는 모두 true
  - 그 밖(Client 역할, DISABLED, 없는 사용자): 0행
  - 고객사는 ACTIVE 만. 비활성 고객사는 app_context_allowed(0001)가 막아 골라도 다른 API 가 모두 0행이다.
  - 정렬: 고객사명, 같으면 company_id
  - 데이터 팀 DDL(v6.2)에는 없는 함수다. glowship-plan DDL 반영은 별도 요청.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0001·v6.2 DDL 과 같은 역할 이름·컨텍스트 식
OWNER_ROLE = "glowship_v4_owner"
APP_ROLE = "glowship_v4_app"
CURRENT_USER_ID = "NULLIF(current_setting('app.user_id', true), '')::uuid"
FUNCTION = "app_me_list_assigned_companies()"

CREATE_SQL = f"""
CREATE FUNCTION app_me_list_assigned_companies()
RETURNS TABLE (company_id uuid, company_name text, can_manage_client_accounts boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $f$
  SELECT c.company_id, c.company_name::text, a.can_manage_client_accounts
    FROM users u
    JOIN admin_assignments a ON a.admin_user_id = u.user_id
    JOIN companies c ON c.company_id = a.company_id
   WHERE u.user_id = {CURRENT_USER_ID}
     AND u.role_code = 'ADMIN' AND u.status = 'ACTIVE' AND c.status = 'ACTIVE'
  UNION ALL
  SELECT c.company_id, c.company_name::text, true
    FROM users u
    CROSS JOIN companies c
   WHERE u.user_id = {CURRENT_USER_ID}
     AND u.role_code = 'OWNER' AND u.status = 'ACTIVE' AND c.status = 'ACTIVE'
   ORDER BY 2, 1
$f$
"""


def upgrade() -> None:
    op.execute(CREATE_SQL)
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {OWNER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}")
