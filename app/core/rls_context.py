import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

"""
    RLS 컨텍스트 설정

    초기 마이그레이션(_create_rls)의 정책·함수는 세션 설정값 app.user_id / app.company_id 를 읽어
    "누가, 어느 고객사 범위에서" 요청하는지 판단한다.
      - 고객사 범위 테이블의 RLS 정책: company_id = app.company_id 인 행만 통과
      - SECURITY DEFINER 함수(app_admin_create_client_user 등): app.user_id 가 해당 고객사에 배정된 ADMIN 인지 검사

    set_config(..., true) 는 현재 트랜잭션에서만 유효하므로(SET LOCAL 과 같음) 요청 단위 세션(get_db)이
    commit/rollback 되면 자동으로 사라진다. 커넥션 풀로 다른 요청에 값이 새지 않는다.

    로컬은 superuser 로 접속해 RLS 정책은 우회되지만, SECURITY DEFINER 함수 안의 검사는 그대로 실행되므로
    함수를 쓰는 경로는 로컬에서도 반드시 컨텍스트를 설정해야 한다.
"""

_SET_RLS_CONTEXT = text(
    "SELECT set_config('app.user_id', :user_id, true),"
    " set_config('app.company_id', :company_id, true)"
)


async def set_rls_context(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    company_id: uuid.UUID | None,
) -> None:
    """
    현재 트랜잭션에 RLS 컨텍스트(app.user_id, app.company_id)를 설정한다.

    정책·함수는 NULLIF(current_setting(..., true), '')::uuid 로 읽으므로 None 은 빈 문자열로 넣는다.
    OWNER·ADMIN 처럼 소속 고객사가 없는 요청자는 company_id 에 "이번 요청이 다루는 고객사"를 넘긴다.
    """
    await db.execute(
        _SET_RLS_CONTEXT,
        {
            "user_id": str(user_id) if user_id is not None else "",
            "company_id": str(company_id) if company_id is not None else "",
        },
    )
