import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.modules.master_data.infrastructure.repository.sku_repository import (
    ExternalCodeRow,
    SkuListRow,
    SkuRepository,
    _like_pattern,
)

COMPANY_ID = uuid.uuid7()
SKU_ID = uuid.uuid7()


@dataclass
class FakeSession:
    """실행한 문장과 파라미터를 기록한다. execute 는 목록 행을, scalar 는 건수를 돌려준다."""

    rows: list = field(default_factory=list)
    total_count: int = 0
    executed: list = field(default_factory=list)

    async def execute(self, statement, params=None):
        self.executed.append((statement, params))
        return iter(self.rows)

    async def scalar(self, statement, params=None):
        self.executed.append((statement, params))
        return self.total_count


def _row(**overrides):
    values = {
        "sku_id": SKU_ID,
        "sku_code": "SKU-201",
        "product_name": "B Bloom Hydra Toner Pad 60ea",
        "brand_name": "B Bloom",
        "category": "SKINCARE",
        "subcategory": "Toner Pad",
        "popularity_tier": "HIT",
        "emergency_air_eligible": True,
        "unit_code": "EA",
        "units_per_case": 24,
        "status": "ACTIVE",
        "external_codes": [
            {
                "source_domain": "INVENTORY",
                "external_code": "WMS-SKU-201",
                "mapping_status": "MAPPED",
            },
            {
                "source_domain": "SHIPMENT",
                "external_code": "TMS-SKU-201",
                "mapping_status": "PENDING",
            },
        ],
        "has_unmapped": True,
        "has_open_issue": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _find(session, **kwargs):
    defaults = {"query": None, "only_errors": False, "offset": 0, "limit": 20}
    return await SkuRepository(session).find_page(COMPANY_ID, **{**defaults, **kwargs})


def _sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


async def test_목록과_건수를_두_문장으로_조회하고_같은_필터를_넘긴다():
    session = FakeSession()

    await _find(session, query="toner", only_errors=True, offset=40, limit=20)

    [(list_stmt, list_params), (count_stmt, count_params)] = session.executed
    assert list_params == {
        "company_id": COMPANY_ID,
        "pattern": "%toner%",
        "only_errors": True,
        "offset": 40,
        "limit": 20,
    }
    assert count_params == {
        "company_id": COMPANY_ID,
        "pattern": "%toner%",
        "only_errors": True,
    }
    list_sql, count_sql = _sql(list_stmt), _sql(count_stmt)
    assert "ORDER BY sku_code, sku_id" in list_sql
    assert "OFFSET" in list_sql and "LIMIT" in list_sql
    assert "SELECT COUNT(*) AS total_count FROM filtered" in count_sql


async def test_SQL_이_외부_코드_집계와_이슈_연결_규칙을_담고_있다():
    session = FakeSession()

    await _find(session)

    sql = _sql(session.executed[0][0])
    # 외부 코드: SKU 매핑만, 원천 순, 매핑 누락 여부
    assert "entity_type = 'SKU'" in sql
    assert "jsonb_agg" in sql and "ORDER BY source_domain, external_code" in sql
    assert "BOOL_OR(mapping_status <> 'MAPPED')" in sql
    # OPEN 이슈: INVENTORY 이슈의 키 둘째 토막을 sku_code 로 본다
    assert "split_part(source_record_key, '|', 2)" in sql
    assert "status = 'OPEN'" in sql
    assert "source_entity_type = 'INVENTORY'" in sql
    # 고객사 조건을 명시한다
    assert "s.company_id = " in sql


async def test_검색어가_없으면_패턴이_NULL_이고_오류_필터는_꺼진다():
    session = FakeSession()

    await _find(session)

    _, params = session.executed[0]
    assert params["pattern"] is None
    assert params["only_errors"] is False


async def test_행을_SkuListRow_로_바꾸고_건수를_함께_돌려준다():
    other_id = uuid.uuid7()
    session = FakeSession(
        rows=[
            _row(),
            _row(
                sku_id=other_id,
                sku_code="SKU-202",
                external_codes=[],
                has_unmapped=False,
                has_open_issue=False,
            ),
        ],
        total_count=20,
    )

    page = await _find(session)

    assert page.total_count == 20
    assert page.rows[0] == SkuListRow(
        sku_id=SKU_ID,
        sku_code="SKU-201",
        product_name="B Bloom Hydra Toner Pad 60ea",
        brand_name="B Bloom",
        category="SKINCARE",
        subcategory="Toner Pad",
        popularity_tier="HIT",
        emergency_air_eligible=True,
        unit_code="EA",
        units_per_case=24,
        status="ACTIVE",
        external_codes=(
            ExternalCodeRow("INVENTORY", "WMS-SKU-201", "MAPPED"),
            ExternalCodeRow("SHIPMENT", "TMS-SKU-201", "PENDING"),
        ),
        has_unmapped=True,
        has_open_issue=True,
    )
    assert page.rows[1].sku_id == other_id
    assert page.rows[1].external_codes == ()
    assert page.rows[1].has_unmapped is False
    assert page.rows[1].has_open_issue is False


async def test_외부_코드가_문자열_JSON_이어도_읽는다():
    session = FakeSession(
        rows=[
            _row(
                external_codes='[{"source_domain": "INVENTORY", "external_code": "X", "mapping_status": "MAPPED"}]'
            )
        ]
    )

    page = await _find(session)

    assert page.rows[0].external_codes == (ExternalCodeRow("INVENTORY", "X", "MAPPED"),)


async def test_건수가_None_이면_0으로_본다():
    session = FakeSession(total_count=None)

    page = await _find(session)

    assert page.total_count == 0


def test_LIKE_특수문자는_글자_그대로_찾도록_이스케이프한다():
    assert _like_pattern(None) is None
    assert _like_pattern("SKU-2") == "%SKU-2%"
    assert _like_pattern("100%") == "%100\\%%"
    assert _like_pattern("a_b") == "%a\\_b%"
    assert _like_pattern("c\\d") == "%c\\\\d%"
