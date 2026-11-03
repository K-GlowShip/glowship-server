from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain

"""
    SKU 코드를 쓰는 원천 유형 (GET /skus 의 externalCodes[] 항목 순서)
      - 상품은 ERP(PRODUCT)가 마스터 원천이라 sku_code 자체가 ERP 코드다. 주문(ORDER)도 주문 상세가 ERP 코드를
        그대로 쓰므로 매핑이 없다. WAREHOUSE 는 창고 엔티티용이다. 그래서 SKU 외부 코드가 필요한 원천은
        재고(INVENTORY = WMS)와 선적(SHIPMENT = TMS) 둘이다.
      - DDL 제약(external_entity_mappings_ck5)상 MAPPED 가 아닌 행은 sku_id 가 비어 있어 SKU 에 붙을 수 없다.
        그래서 "이 SKU 에 어느 원천 코드가 없는가"는 이 목록을 기준으로 MAPPED 행의 부재로 판정한다.
      - 응답은 이 순서로 원천마다 항목 하나를 항상 내려주고, 매핑이 없으면 externalCode = null, mappingStatus = UNMAPPED 다.
      - 고객사별 원천 구성이 생기면 이 상수를 설정 값으로 바꾼다.
"""

SKU_CODE_SOURCE_DOMAINS: tuple[SourceDomain, ...] = (
    SourceDomain.INVENTORY,
    SourceDomain.SHIPMENT,
)
