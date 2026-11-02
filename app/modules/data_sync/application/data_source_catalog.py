from dataclasses import dataclass
from enum import Enum

from app.modules.data_sync.infrastructure.data_sync_enums import SourceDomain

"""
    연동 데이터 카탈로그 (연동 데이터 조회 화면의 카드 목록)
      - 화면에 보여줄 수집 도메인과 그 표시 정보를 고정한다. DB 에 연동 설정 테이블이 없어 서버 상수로 둔다.
      - 외부 시스템 종류는 도메인별 고정 매핑이다 (ORDER→ERP, INVENTORY→WMS, SHIPMENT→TMS).
      - 갱신 주기는 수집 워커가 아직 없어 기본값(10분)으로 둔다. 고객사별 설정이 생기면 이 값을 대체한다.
      - 마스터 데이터 도메인(PRODUCT·WAREHOUSE)은 화면 범위 밖이라 넣지 않는다.
"""

DEFAULT_UPDATE_INTERVAL_SECONDS = 600


class ExternalSystem(str, Enum):
    """고객사가 연동한 외부 시스템 종류. DB 코드값이 아니라 화면 표시용 값이다."""

    ERP = "ERP"
    WMS = "WMS"
    TMS = "TMS"


@dataclass(frozen=True)
class DataSourceSpec:
    """카드 한 장의 고정 정보"""

    source_domain: SourceDomain
    system_name: ExternalSystem
    display_name: str
    update_interval_seconds: int = DEFAULT_UPDATE_INTERVAL_SECONDS


# 응답 순서 = 화면 카드 순서
CARD_DATA_SOURCES: tuple[DataSourceSpec, ...] = (
    DataSourceSpec(SourceDomain.ORDER, ExternalSystem.ERP, "주문 데이터"),
    DataSourceSpec(SourceDomain.INVENTORY, ExternalSystem.WMS, "재고 데이터"),
    DataSourceSpec(SourceDomain.SHIPMENT, ExternalSystem.TMS, "선적 데이터"),
)
