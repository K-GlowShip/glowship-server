import math
from collections.abc import Sequence
from typing import Annotated, Any, Self

from fastapi import Query
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.schemas import CamelModel

"""
    목록 페이지네이션 공통 부품 (API 명세 1절 "목록은 q·page·size", D11)
      - 요청: page(1부터, 기본 1), size(기본 20, 최대 100)
      - 응답: items, page, size, totalCount(필터를 모두 적용한 전체 개수), totalPages
      - 라우터:     params: PageParams = Depends()
      - 응답 모델:  Page[MyItemResponse]  (추가 필드가 필요하면 상속해 필드를 더합니다)
      - 응답 생성:  Page[MyItemResponse].of(items, total_count, params)
      - ORM select 문이면 paginate(session, stmt, params) 로 개수 조회와 LIMIT·OFFSET 을 한 번에 처리합니다.
        text SQL 이면 params.limit·params.offset 을 SQL 에 넣고, 같은 조건으로 센 개수를 Page.of 에 넘깁니다.
"""

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class PageParams:
    """페이지 요청 값. FastAPI 의존성으로 쓰면 쿼리 파라미터 page·size 를 검증해 받습니다."""

    def __init__(
        self,
        page: Annotated[int, Query(ge=1, description="페이지 번호 (1부터)")] = 1,
        size: Annotated[
            int,
            Query(
                ge=1,
                le=MAX_PAGE_SIZE,
                description=f"페이지 크기 (기본 {DEFAULT_PAGE_SIZE}, 최대 {MAX_PAGE_SIZE})",
            ),
        ] = DEFAULT_PAGE_SIZE,
    ):
        self.page = page
        self.size = size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        return self.size

    def __repr__(self) -> str:
        return f"PageParams(page={self.page}, size={self.size})"


class Page[T](CamelModel):
    """목록 응답 공통 형태"""

    items: list[T]
    page: int
    size: int
    total_count: int
    total_pages: int

    @classmethod
    def of(
        cls, items: Sequence[T], total_count: int, params: PageParams, **extra: Any
    ) -> Self:
        """totalPages 는 totalCount 와 size 로 계산합니다 (0건이면 0). extra 는 상속한 응답의 추가 필드입니다."""
        return cls(
            items=list(items),
            page=params.page,
            size=params.size,
            total_count=total_count,
            total_pages=math.ceil(total_count / params.size),
            **extra,
        )


async def paginate(
    session: AsyncSession, stmt: Select, params: PageParams
) -> tuple[list[Any], int]:
    """
    ORM select 문의 한 페이지와 전체 개수를 반환합니다. 정렬은 stmt 에 ORDER BY 로 지정해 두어야 합니다.
        rows, total_count = await paginate(session, select(Order).order_by(Order.ordered_at.desc()), params)
    """
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total_count = await session.scalar(count_stmt)
    rows = await session.scalars(stmt.limit(params.limit).offset(params.offset))
    return list(rows), total_count or 0
