import uuid

from sqlalchemy import text

from app.modules.master_data.infrastructure.repository.sku_repository import (
    SkuRepository,
)
from app.modules.master_data.infrastructure.sku_code_sources import (
    SKU_CODE_SOURCE_DOMAINS,
)

"""
    SkuRepository SQL 스모크 테스트 (실제 DB, 읽기 전용, 롤백)
      - 단위 테스트는 가짜 세션으로만 돌기 때문에, 여기서 SQL 자체가 실행되고 결과 형태가 맞는지 확인합니다.
      - 빈 DB 에서도 SQL 은 실행되어야 하므로, 데이터에 기대는 확인은 데이터가 있을 때만 합니다.
      - DB 가 없으면 db_session 픽스처가 건너뜁니다 (tests/conftest.py).
"""

UNKNOWN_ID = uuid.UUID("00000000-0000-7000-8000-000000000000")


async def _sample_company(db) -> uuid.UUID:
    """상품이 있는 고객사, 없으면 없는 ID"""
    company_id = await db.scalar(
        text("SELECT company_id FROM skus ORDER BY sku_code LIMIT 1")
    )
    return company_id or UNKNOWN_ID


async def test_빈_고객사는_빈_페이지와_0건이다(db_session):
    page = await SkuRepository(db_session).find_page(
        UNKNOWN_ID, query=None, only_errors=False, offset=0, limit=20
    )

    assert page.rows == []
    assert page.total_count == 0


async def test_목록은_sku_code_순이고_건수는_필터와_맞는다(db_session):
    repo = SkuRepository(db_session)
    company_id = await _sample_company(db_session)

    page = await repo.find_page(
        company_id, query=None, only_errors=False, offset=0, limit=100
    )
    errors = await repo.find_page(
        company_id, query=None, only_errors=True, offset=0, limit=100
    )
    beyond = await repo.find_page(
        company_id, query=None, only_errors=False, offset=10_000, limit=20
    )

    codes = [row.sku_code for row in page.rows]
    assert codes == sorted(codes)
    assert page.total_count >= len(page.rows)
    assert errors.total_count <= page.total_count
    assert all(row.has_unmapped or row.has_open_issue for row in errors.rows)
    assert beyond.rows == [] and beyond.total_count == page.total_count
    expected_domains = [domain.value for domain in SKU_CODE_SOURCE_DOMAINS]
    for row in page.rows:
        # SKU 코드 원천마다 항목이 항상 하나씩, 고정 순서. 비면 externalCode None·UNMAPPED 이고 매핑 누락이다
        assert [code.source_domain for code in row.external_codes] == expected_domains
        assert row.has_unmapped is any(
            code.external_code is None for code in row.external_codes
        )
        for code in row.external_codes:
            assert (code.external_code is None) == (code.mapping_status == "UNMAPPED")
        assert row.hs10


async def test_검색어는_상품명과_코드에_부분_일치한다(db_session):
    repo = SkuRepository(db_session)
    company_id = await _sample_company(db_session)
    page = await repo.find_page(
        company_id, query=None, only_errors=False, offset=0, limit=1
    )
    if not page.rows:
        return
    needle = page.rows[0].sku_code[-3:].lower()

    found = await repo.find_page(
        company_id, query=needle, only_errors=False, offset=0, limit=100
    )

    assert found.total_count >= 1
    assert all(
        needle in row.sku_code.lower() or needle in row.product_name.lower()
        for row in found.rows
    )


async def test_합성데이터_B사_SKU_201_은_재고_이슈가_열려_있다(db_session):
    """합성데이터(glowship_synthetic_2025_v5)가 적재된 DB 에서만 확인한다."""
    company_id = await db_session.scalar(
        text("SELECT company_id FROM companies WHERE company_code = 'B_COSMETICS'")
    )
    has_issue = await db_session.scalar(
        text(
            "SELECT 1 FROM data_validation_issues"
            " WHERE source_record_key = 'WMS-ATL01|SKU-201|2025-12-31' LIMIT 1"
        )
    )
    if company_id is None or has_issue is None:
        return

    page = await SkuRepository(db_session).find_page(
        company_id, query="SKU-201", only_errors=False, offset=0, limit=10
    )

    [row] = [r for r in page.rows if r.sku_code == "SKU-201"]
    assert row.has_open_issue is True

    # 매핑 테이블까지 적재된 DB 에서만: SKU 당 WMS·TMS 코드 하나씩, 원천 순, 매핑 누락 없음
    has_mapping = await db_session.scalar(
        text(
            "SELECT 1 FROM external_entity_mappings"
            " WHERE company_id = :c AND entity_type = 'SKU' LIMIT 1"
        ),
        {"c": company_id},
    )
    if has_mapping:
        assert [(c.source_domain, c.mapping_status) for c in row.external_codes] == [
            ("INVENTORY", "MAPPED"),
            ("SHIPMENT", "MAPPED"),
        ]
        assert row.has_unmapped is False
