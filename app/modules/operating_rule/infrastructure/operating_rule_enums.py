from enum import Enum


class LeadTimeType(str, Enum):
    """리드타임 유형. operating_lead_times.lead_time_type CHECK 제약(operating_lead_times_ck1)과 같은 값이다."""

    CUSTOMER_DELIVERY = "CUSTOMER_DELIVERY"
    AIR_TRANSIT = "AIR_TRANSIT"
    INBOUND_HANDLING = "INBOUND_HANDLING"
    OUTBOUND_HANDLING = "OUTBOUND_HANDLING"


# 도착 권역(to_region_code)이 있어야 하는 유형. 나머지 유형은 비워야 한다(operating_lead_times_ck4)
REGION_REQUIRED_LEAD_TIME_TYPES = frozenset(
    {LeadTimeType.CUSTOMER_DELIVERY, LeadTimeType.AIR_TRANSIT}
)


class DayBasis(str, Enum):
    """일수 기준. operating_lead_times.day_basis CHECK 제약(operating_lead_times_ck2)과 같은 값이다."""

    CALENDAR_DAY = "CALENDAR_DAY"
    BUSINESS_DAY = "BUSINESS_DAY"
