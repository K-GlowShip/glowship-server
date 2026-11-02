from dataclasses import dataclass

from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain

"""
    연동 데이터 카탈로그 (GET /data-sync-runs/latest 가 돌려주는 원천 유형 목록)
      - 명세 v6.2 4.3: 원천 유형 5개(PRODUCT·WAREHOUSE·ORDER·SHIPMENT·INVENTORY)별 마지막 수집 1건.
      - 원천 시스템 이름과 수집 주기는 운영 설정 값이라 DB 에 없다. 서버 상수로 두고 응답에 함께 내려준다
        (sourceSystemName, syncIntervalMinutes). 고객사별 설정이 생기면 이 값을 대체한다.
      - 원천 시스템은 도메인별 고정 매핑이다. 상품 마스터는 ERP, 창고 마스터는 WMS 에서 온다고 본다.
      - 응답 순서 = 화면 카드 순서(주문·재고·선적)를 앞에, 마스터 데이터(상품·창고)를 뒤에 둔다.
"""

DEFAULT_SYNC_INTERVAL_MINUTES = 10


@dataclass(frozen=True)
class DataSourceSpec:
    """원천 유형 하나의 고정 정보"""

    source_domain: SourceDomain
    source_system_name: str
    sync_interval_minutes: int = DEFAULT_SYNC_INTERVAL_MINUTES


DATA_SOURCES: tuple[DataSourceSpec, ...] = (
    DataSourceSpec(SourceDomain.ORDER, "SAP ERP"),
    DataSourceSpec(SourceDomain.INVENTORY, "WMS"),
    DataSourceSpec(SourceDomain.SHIPMENT, "TMS"),
    DataSourceSpec(SourceDomain.PRODUCT, "SAP ERP"),
    DataSourceSpec(SourceDomain.WAREHOUSE, "WMS"),
)
