import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Page,
    PageParams,
    paginate,
)
from app.core.schemas import CamelModel
from app.modules.company.infrastructure.company import Company


class ItemResponse(CamelModel):
    item_name: str


class ItemPageWithCounts(Page[ItemResponse]):
    extra_count: int


# --- PageParams ------------------------------------------------------------------


def test_page_params_defaults_and_offset():
    params = PageParams()

    assert (params.page, params.size) == (1, DEFAULT_PAGE_SIZE)
    assert (params.offset, params.limit) == (0, DEFAULT_PAGE_SIZE)
    assert PageParams(page=3, size=10).offset == 20


_app = FastAPI()


@_app.get("/items")
async def _list_items(params: PageParams = Depends()) -> dict:
    return {"page": params.page, "size": params.size}


async def _get(query: dict):
    async with AsyncClient(
        transport=ASGITransport(app=_app), base_url="http://test"
    ) as client:
        return await client.get("/items", params=query)


async def test_page_params_as_dependency_reads_query():
    response = await _get({"page": 2, "size": 50})

    assert response.json() == {"page": 2, "size": 50}


@pytest.mark.parametrize(
    "query", [{"page": 0}, {"size": 0}, {"size": MAX_PAGE_SIZE + 1}, {"page": "x"}]
)
async def test_page_params_rejects_out_of_range(query):
    response = await _get(query)

    assert response.status_code == 422


# --- Page ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total_count", "size", "total_pages"),
    [(0, 20, 0), (1, 20, 1), (20, 20, 1), (21, 20, 2), (100, 30, 4)],
)
def test_page_total_pages(total_count, size, total_pages):
    page = Page[ItemResponse].of([], total_count, PageParams(page=1, size=size))

    assert page.total_pages == total_pages


def test_page_serializes_in_camel_case():
    items = [ItemResponse(item_name="a")]

    page = Page[ItemResponse].of(items, 21, PageParams(page=2, size=20))

    assert page.model_dump() == {
        "items": [{"itemName": "a"}],
        "page": 2,
        "size": 20,
        "totalCount": 21,
        "totalPages": 2,
    }


def test_subclassed_page_takes_extra_fields():
    page = ItemPageWithCounts.of([], 0, PageParams(), extra_count=5)

    assert page.model_dump()["extraCount"] == 5
    assert page.total_pages == 0


# --- paginate (실제 DB, 없으면 건너뜀) --------------------------------------------


async def test_paginate_returns_page_and_total_count(db_session):
    stmt = select(Company).order_by(Company.company_id)
    all_rows = list(await db_session.scalars(stmt))

    first, total_count = await paginate(db_session, stmt, PageParams(page=1, size=1))
    beyond, _ = await paginate(
        db_session, stmt, PageParams(page=len(all_rows) + 1, size=1)
    )

    assert total_count == len(all_rows)
    assert first == all_rows[:1]
    assert beyond == []
