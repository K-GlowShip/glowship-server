"""grant worker UPDATE on delay_events.closed_at·close_reason (episode close)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-05 11:00:00

지연 에피소드 종료(close_episodes, GLOW-16)는 SHIPMENT 수집 트랜잭션 안에서 워커 역할로 delay_events 의
closed_at·close_reason 을 기록한다. v6.2 DDL 과 0001 은 워커에게 delay_events 조회·추가만 주므로(UPDATE 없음)
워커 역할로 연결하면 회복·입고 종료 때 권한 오류가 나고 SHIPMENT 수집 전체가 롤백된다(GLOW-117).
  - 결정(glowship-plan cd28186, ERD 설명 4절 "역할·권한"): 워커에게 두 열만 UPDATE 를 준다.
    시작 값(단계·계획·예상 입고일, episode_no 등)은 고치지 않는다는 규칙("종료는 시각·사유를 한 번만 기록")과 같은 범위다.
  - RLS 정책 delay_events_tenant 는 이미 워커에게 FOR ALL 이라 정책은 바꾸지 않는다(고객사 범위 조건은 그대로 걸림).
  - 같은 GRANT 를 다시 실행해도 결과가 같다(이미 권한이 있으면 바뀌는 것 없음).
  - downgrade 는 두 열의 UPDATE 만 거둔다. 데이터 팀 DDL 새 판이 이 권한을 직접 주게 된 DB 에서 downgrade 하면
    그 권한도 사라지므로, 그때는 이 리비전을 내리지 않는다.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0001·v6.2 DDL 과 같은 역할 이름
WORKER_ROLE = "glowship_v4_worker"
TABLE = "delay_events"
CLOSE_COLUMNS = ("closed_at", "close_reason")

GRANT_SQL = f"GRANT UPDATE ({', '.join(CLOSE_COLUMNS)}) ON {TABLE} TO {WORKER_ROLE}"
REVOKE_SQL = f"REVOKE UPDATE ({', '.join(CLOSE_COLUMNS)}) ON {TABLE} FROM {WORKER_ROLE}"


def upgrade() -> None:
    op.execute(GRANT_SQL)


def downgrade() -> None:
    op.execute(REVOKE_SQL)
