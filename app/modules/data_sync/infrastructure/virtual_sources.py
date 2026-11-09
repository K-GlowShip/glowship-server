import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain
from app.modules.data_sync.infrastructure.virtual_tms import (
    VirtualTmsScenarioError,
    _require_aware,
)

"""
    가상 원천 시나리오: TMS(선적)·ERP(상품·주문)·WMS(창고·재고) (GLOW-13·49, 2026-11-09 결정)
      - 실제 원천 대신, 관측 시각(fetched_at)이 붙은 원천 응답을 담은 시나리오 파일을 읽어 "그 구간에 관측된 레코드"를
        원천별로 돌려줍니다. 수동 동기화(고객사 tick)가 다섯 원천을 같은 구간으로 묻습니다.
      - 파일 모양: {"responses": [{"fetched_at": "…Z", "_demo_company": "A_BEAUTY", "source_domain": "ORDER", "data": [...]}, ...]}
          · source_domain 이 없으면 SHIPMENT(기존 가상 TMS 시나리오 파일, virtual_tms 와 같은 모양)
          · data 레코드 모양은 원천 파서가 읽는 모양입니다: SHIPMENT 는 TMS 응답(tms_parser), ORDER·INVENTORY 는
            수집 레코드와 같은 snake_case(erp_wms_parser). PRODUCT·WAREHOUSE 응답은 지금 데모 데이터에 없습니다(마스터 변경 없음).
          · 여러 파일을 합쳐 읽습니다(가상 TMS 시나리오 + census extend_days 결과).
      - records_between(고객사, 원천, after, until): after < fetched_at ≤ until 인 응답의 레코드를 관측 시각 순서대로 모아
        같은 키는 가장 늦게 관측된 값 하나만 남깁니다(한 수집에 같은 키가 두 번이면 적재가 첫 레코드만 넣으므로).
        키: SHIPMENT shipmentNo, ORDER order_code, INVENTORY (inventory_date, warehouse_code, sku_code). 키를 읽을 수 없는
        레코드는 합치지 않고 그대로 넘겨 파서가 거부하게 합니다.
      - 시각은 호출 측이 7월 초 가상 시각으로 넘깁니다(서버의 지금 시각이 아님).
"""

_KEYS: dict[SourceDomain, tuple[str, ...]] = {
    SourceDomain.SHIPMENT: ("shipmentNo",),
    SourceDomain.ORDER: ("order_code",),
    SourceDomain.INVENTORY: ("inventory_date", "warehouse_code", "sku_code"),
}


@dataclass(frozen=True)
class SourceResponse:
    fetched_at: datetime
    company_code: str
    source_domain: SourceDomain
    data: tuple[Mapping[str, Any], ...]


class VirtualSourceScenario:
    def __init__(self, responses: Sequence[SourceResponse]):
        # 같은 시각의 응답은 읽은 순서를 지킵니다 (안정 정렬)
        self.responses = tuple(sorted(responses, key=lambda r: r.fetched_at))

    @classmethod
    def from_files(cls, paths: Sequence[str | Path]) -> VirtualSourceScenario:
        responses: list[SourceResponse] = []
        for path in paths:
            with open(path, encoding="utf-8") as file:
                responses.extend(cls.from_json(json.load(file)).responses)
        return cls(responses)

    @classmethod
    def from_json(cls, document: Any) -> VirtualSourceScenario:
        if not isinstance(document, Mapping) or not isinstance(
            document.get("responses"), list
        ):
            raise VirtualTmsScenarioError("시나리오 최상위에 responses 배열이 없음")
        return cls(
            [_response(raw, index) for index, raw in enumerate(document["responses"])]
        )

    def domains(self) -> set[SourceDomain]:
        return {r.source_domain for r in self.responses}

    def records_between(
        self,
        company_code: str,
        source_domain: SourceDomain,
        after: datetime | None,
        until: datetime,
    ) -> list[Mapping[str, Any]]:
        _require_aware(until, "until")
        if after is not None:
            _require_aware(after, "after")
        key_fields = _KEYS.get(source_domain, ())
        latest: dict[Any, Mapping[str, Any]] = {}
        for response in self.responses:
            if (
                response.company_code != company_code
                or response.source_domain != source_domain
                or response.fetched_at > until
                or (after is not None and response.fetched_at <= after)
            ):
                continue
            for index, record in enumerate(response.data):
                key = _key(record, key_fields) or (response.fetched_at, index)
                latest.pop(key, None)
                latest[key] = record
        return list(latest.values())


def _key(record: Any, fields: tuple[str, ...]) -> tuple | None:
    if not fields or not isinstance(record, Mapping):
        return None
    values = tuple(record.get(f) for f in fields)
    if all(isinstance(v, str) and v for v in values):
        return values
    return None


def _response(raw: Any, index: int) -> SourceResponse:
    where = f"responses[{index}]"
    if not isinstance(raw, Mapping):
        raise VirtualTmsScenarioError(f"{where} 가 JSON 객체가 아님")
    company = raw.get("_demo_company")
    if not isinstance(company, str) or not company.strip():
        raise VirtualTmsScenarioError(f"{where}._demo_company(고객사 코드) 없음")
    domain_raw = raw.get("source_domain", SourceDomain.SHIPMENT.value)
    try:
        domain = SourceDomain(domain_raw)
    except ValueError:
        raise VirtualTmsScenarioError(
            f"{where}.source_domain 이 원천 유형이 아님 ({domain_raw!r})"
        ) from None
    fetched = raw.get("fetched_at")
    try:
        fetched_at = (
            datetime.fromisoformat(fetched) if isinstance(fetched, str) else None
        )
    except ValueError:
        fetched_at = None
    if fetched_at is None or fetched_at.tzinfo is None:
        raise VirtualTmsScenarioError(
            f"{where}.fetched_at 을 시간대 있는 ISO 8601 시각으로 읽을 수 없음 ({fetched!r})"
        )
    data = raw.get("data")
    if not isinstance(data, list):
        raise VirtualTmsScenarioError(f"{where}.data 가 배열이 아님")
    return SourceResponse(
        fetched_at=fetched_at,
        company_code=company,
        source_domain=domain,
        data=tuple(data),
    )
