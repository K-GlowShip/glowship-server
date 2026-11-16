"""add client user password reset function app_admin_reset_client_user_password (SECURITY DEFINER)

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-16 15:00:00

Admin 이 담당 고객사 Client 계정의 비밀번호를 대신 재설정한다(명세 3절 29번, C47, 프론트 GLOW-103).
앱 역할(glowship_v4_app)은 users 에서 본인 display_name 만 UPDATE 할 수 있고(0001 GRANT·users_update_self),
password_hash 를 바꾸는 함수가 없다(app_admin_update_client_user 는 역할·상태만 바꾼다).
  - 0001 app_admin_create_client_user 와 같은 방식: 소유자 역할이 가진 SECURITY DEFINER 함수가 대신 UPDATE 하고,
    실행 권한은 앱 역할에만 준다(PUBLIC·워커 없음).
  - app.company_id 를 쓰지 않는다. 요청은 userId 만 주므로 대상 계정의 고객사를 호출하는 쪽이 모른다.
    함수가 대상 계정의 고객사를 읽고 요청자(app.user_id)의 배정을 직접 확인한다.
  - 오류 구분(명세 4.1 계정 관리 공통 규칙). 서버가 SQLSTATE 로 HTTP 오류를 가른다.
      * insufficient_privilege(42501) → 403: 요청자가 ACTIVE ADMIN 이 아님, 배정은 있으나 계정 관리 권한 없음
      * no_data_found(P0002) → 404: 대상 계정이 없음, Client 역할이 아님, 요청자가 그 고객사에 배정되지 않음(구분 없음)
      * invalid_parameter_value(22023): BCrypt 해시가 아님(서버 버그일 때만, 평문 저장 방지)
  - 고객사 상태와 대상 계정 상태(DISABLED)는 보지 않는다. 해시만 바뀌고 로그인 가능 여부는 로그인이 판정한다.
  - 이미 발급된 토큰은 그대로다(명세 D26). 폐기할 저장소가 없다(C44).
  - 데이터 팀 DDL(v6.2)에는 없는 함수다. glowship-plan DDL 반영은 별도 요청.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0001·v6.2 DDL 과 같은 역할 이름·컨텍스트 식
OWNER_ROLE = "glowship_v4_owner"
APP_ROLE = "glowship_v4_app"
CURRENT_USER_ID = "NULLIF(current_setting('app.user_id', true), '')::uuid"
FUNCTION = "app_admin_reset_client_user_password(uuid, text)"

# BCrypt 정규식은 0001 app_admin_create_client_user 와 같다.
CREATE_SQL = f"""
CREATE FUNCTION app_admin_reset_client_user_password(p_user_id uuid, p_password_hash text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $f$
DECLARE
  v_actor_id uuid := {CURRENT_USER_ID};
  v_company_id uuid;
  v_can_manage boolean;
BEGIN
  IF p_password_hash !~ '^\\$2[aby]\\$[0-9]{{2}}\\$[./A-Za-z0-9]{{53}}$' THEN
    RAISE EXCEPTION 'BCrypt 해시가 아님' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM users u WHERE u.user_id = v_actor_id AND u.role_code = 'ADMIN' AND u.status = 'ACTIVE') THEN
    RAISE EXCEPTION '비밀번호 재설정 권한 없음: ACTIVE ADMIN 만 가능' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT u.company_id INTO v_company_id FROM users u
   WHERE u.user_id = p_user_id AND u.role_code IN ('CLIENT_MEMBER', 'CLIENT_LEAD');
  IF NOT FOUND THEN
    RAISE EXCEPTION '담당 고객사의 Client 사용자가 아님' USING ERRCODE = 'no_data_found';
  END IF;
  SELECT a.can_manage_client_accounts INTO v_can_manage FROM admin_assignments a
   WHERE a.admin_user_id = v_actor_id AND a.company_id = v_company_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION '담당 고객사의 Client 사용자가 아님' USING ERRCODE = 'no_data_found';
  END IF;
  IF NOT v_can_manage THEN
    RAISE EXCEPTION '비밀번호 재설정 권한 없음: 계정 관리 권한이 있는 배정만 가능' USING ERRCODE = 'insufficient_privilege';
  END IF;
  UPDATE users SET password_hash = p_password_hash, updated_at = now() WHERE user_id = p_user_id;
END $f$
"""


def upgrade() -> None:
    op.execute(CREATE_SQL)
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"ALTER FUNCTION {FUNCTION} OWNER TO {OWNER_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}")
