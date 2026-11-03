import uuid
from dataclasses import dataclass, field

from app.core.pagination import PageParams
from app.core.security.tenant import TenantContext
from app.modules.master_data.application.sku_list_service import (
    SkuListService,
    SkuValidationStatus,
)
from app.modules.master_data.infrastructure.repository.sku_repository import (
    ExternalCodeRow,
    SkuListPage,
    SkuListRow,
)

TENANT = TenantContext(user_id=uuid.uuid7(), company_id=uuid.uuid7())


@dataclass
class FakeSkuRepository:
    page: SkuListPage = field(
        default_factory=lambda: SkuListPage(rows=[], total_count=0)
    )
    calls: list = field(default_factory=list)

    async def find_page(self, company_id, **kwargs):
        self.calls.append((company_id, kwargs))
        return self.page


def _row(**overrides) -> SkuListRow:
    values = {
        "sku_id": uuid.uuid7(),
        "sku_code": "SKU-001",
        "product_name": "A Glow Hydra Toner Pad 60ea",
        "brand_name": "A Glow",
        "hs10": "3304995000",
        "category": "SKINCARE",
        "subcategory": "Toner Pad",
        "popularity_tier": "HIT",
        "emergency_air_eligible": True,
        "unit_code": "EA",
        "units_per_case": 24,
        "status": "ACTIVE",
        "external_codes": (
            ExternalCodeRow("INVENTORY", "WMS-SKU-001", "MAPPED"),
            ExternalCodeRow("SHIPMENT", "TMS-SKU-001", "MAPPED"),
        ),
        "has_unmapped": False,
        "has_open_issue": False,
    }
    values.update(overrides)
    return SkuListRow(**values)


def _service(rows: list[SkuListRow], total_count: int = 0):
    repo = FakeSkuRepository(page=SkuListPage(rows=rows, total_count=total_count))
    return SkuListService(repo=repo), repo


async def test_페이지를_offset_limit_으로_바꿔_고객사_범위로_조회한다():
    service, repo = _service([])

    await service.get_page(
        TENANT, query="toner", only_errors=True, page=PageParams(page=3, size=25)
    )

    [(company_id, kwargs)] = repo.calls
    assert company_id == TENANT.company_id
    assert kwargs == {"query": "toner", "only_errors": True, "offset": 50, "limit": 25}


async def test_page_를_생략하면_첫_페이지_기본_크기다():
    service, repo = _service([])

    result = await service.get_page(TENANT)

    [(_, kwargs)] = repo.calls
    assert kwargs == {"query": None, "only_errors": False, "offset": 0, "limit": 20}
    assert (result.page.page, result.page.size) == (1, 20)


async def test_검색어는_공백을_지우고_비면_없는_것으로_본다():
    service, repo = _service([])

    await service.get_page(TENANT, query="  SKU-2  ")
    await service.get_page(TENANT, query="   ")

    assert [call[1]["query"] for call in repo.calls] == ["SKU-2", None]


async def test_건수와_페이지_정보를_그대로_돌려준다():
    service, _ = _service([], total_count=128)
    page = PageParams(page=2, size=50)

    result = await service.get_page(TENANT, page=page)

    assert result.items == []
    assert result.total_count == 128
    assert result.page is page


async def test_행의_필드와_외부_코드를_항목으로_옮긴다():
    row = _row()
    service, _ = _service([row])

    [item] = (await service.get_page(TENANT)).items

    assert item.sku_id == row.sku_id
    assert item.sku_code == "SKU-001"
    assert item.product_name == row.product_name
    assert item.brand_name == "A Glow"
    assert item.hs10 == "3304995000"
    assert item.category == "SKINCARE"
    assert item.subcategory == "Toner Pad"
    assert item.popularity_tier == "HIT"
    assert item.is_emergency_air_eligible is True
    assert item.unit_code == "EA"
    assert item.units_per_case == 24
    assert item.status == "ACTIVE"
    assert item.external_codes == row.external_codes
    assert item.validation_status == SkuValidationStatus.OK


async def test_매핑이_없는_원천이_있으면_CODE_MISSING_이다():
    row = _row(
        external_codes=(
            ExternalCodeRow("INVENTORY", "WMS-SKU-001", "MAPPED"),
            ExternalCodeRow("SHIPMENT", None, "UNMAPPED"),
        ),
        has_unmapped=True,
    )
    service, _ = _service([row])

    [item] = (await service.get_page(TENANT)).items

    assert item.validation_status == SkuValidationStatus.CODE_MISSING
    assert item.external_codes[1].external_code is None


async def test_열린_이슈가_있으면_ISSUE_OPEN_이다():
    service, _ = _service([_row(has_open_issue=True)])

    [item] = (await service.get_page(TENANT)).items

    assert item.validation_status == SkuValidationStatus.ISSUE_OPEN


async def test_둘_다면_CODE_MISSING_이_우선이다():
    service, _ = _service([_row(has_unmapped=True, has_open_issue=True)])

    [item] = (await service.get_page(TENANT)).items

    assert item.validation_status == SkuValidationStatus.CODE_MISSING
