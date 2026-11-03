from enum import Enum


class OrderStatus(str, Enum):
    """주문 상태. orders.status CHECK 제약(orders_ck3)과 같은 값이다.
    OPEN 에는 재고 미확보 주문과 재고를 확보하고 다음 날 출고를 기다리는 주문이 함께 있다 (명세 4.3 주의, D8)."""

    OPEN = "OPEN"
    PARTIALLY_SHIPPED = "PARTIALLY_SHIPPED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    REMOVED_AT_SOURCE = "REMOVED_AT_SOURCE"


class SalesChannel(str, Enum):
    """판매 채널. orders.sales_channel CHECK 제약(orders_ck2)과 같은 값이다."""

    AMAZON = "AMAZON"
    SEPHORA = "SEPHORA"


class CustomerRegion(str, Enum):
    """고객 권역. orders.customer_region CHECK 제약(orders_ck1)과 같은 값이다. 화면 표시명은 서부·동부."""

    WEST = "WEST"
    EAST = "EAST"


class OrderLineStatus(str, Enum):
    """주문 상세 상태. order_lines.line_status CHECK 제약(order_lines_ck3)과 같은 값이다."""

    ACTIVE = "ACTIVE"
    REMOVED_AT_SOURCE = "REMOVED_AT_SOURCE"
