import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

"""
    지연 화면 표시값 계산 (API 명세 D13, 4.4절 GET /delay-events)
      - 경로(route): 한국 출고 창고의 지역 → 도착 항구 이름. 예: "KR → LOS ANGELES"
      - 대표 상품: 선적에서 수량이 가장 많은 SKU. 수량이 같으면 상세 줄 번호가 작은 SKU
      - 지연 일수: 예상 3PL 입고일 − 계획 3PL 입고일 (일)
      - DB 조회 없이 값만 받는 순수 함수입니다. 지연 조회 API와 분석 실행 조회(개발자 3)가 같이 씁니다.
"""

ROUTE_ARROW = " → "


@dataclass(frozen=True)
class ShipmentLineQuantity:
    """대표 상품 계산에 쓰는 선적 상세 1줄"""

    line_no: int
    sku_id: uuid.UUID
    shipped_quantity: int


def build_route(origin_region_code: str, us_port_name: str) -> str:
    """
    경로 표시 문자열. 도착 항구 이름 끝의 주 약칭(", CA")은 뺍니다.
        build_route("KR", "LOS ANGELES, CA") == "KR → LOS ANGELES"
    """
    port = us_port_name.split(",", 1)[0].strip()
    return f"{origin_region_code}{ROUTE_ARROW}{port}"


def pick_representative_line(
    lines: Iterable[ShipmentLineQuantity],
) -> ShipmentLineQuantity | None:
    """수량이 가장 많은 상세 줄. 수량이 같으면 줄 번호가 작은 쪽, 상세가 없으면 None"""
    return min(
        lines, key=lambda line: (-line.shipped_quantity, line.line_no), default=None
    )


def count_skus(lines: Iterable[ShipmentLineQuantity]) -> int:
    """선적에 실린 서로 다른 SKU 수"""
    return len({line.sku_id for line in lines})


def calculate_delay_days(
    planned_3pl_inbound_date: date, expected_3pl_inbound_date: date
) -> int:
    """
    예상 3PL 입고일이 계획보다 며칠 늦은지. 지연 화면은 선적의 현재 예상일을 쓰므로,
    회복되어 예상일이 계획일 이하가 되면 0 입니다 (음수로 내리지 않음).
    """
    return max((expected_3pl_inbound_date - planned_3pl_inbound_date).days, 0)
