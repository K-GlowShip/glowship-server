from enum import Enum


class TransportMode(str, Enum):
    """운송 수단. shipments.transport_mode CHECK 제약(shipments_ck1)과 같은 값이다. 화면 표시명은 해상·항공."""

    OCEAN = "OCEAN"
    AIR = "AIR"


class ShipmentStage(str, Enum):
    """선적의 현재 운송 단계. shipments.current_stage CHECK 제약(shipments_ck2)과 같은 값이다.
    화면 표시명은 명세 2절: 출항 전·해상 운송·항공 운송·항구 도착·통관 중·입고 완료.
    RECEIVED 이면 actual_3pl_inbound_date 가 반드시 있다 (shipments_ck3)."""

    PRE_DEPARTURE = "PRE_DEPARTURE"
    AT_SEA = "AT_SEA"
    IN_AIR = "IN_AIR"
    AT_PORT = "AT_PORT"
    RECEIVED = "RECEIVED"
