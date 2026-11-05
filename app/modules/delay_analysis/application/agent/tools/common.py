import re
from collections import Counter
from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    StrictStr,
    StringConstraints,
)

"""
    에이전트 도구 입출력 공통 규칙 (docs/agent-flow/tool-contracts.md 1절)
      - JSON 키는 snake_case 그대로, 알 수 없는 키는 받지 않는다(extra="forbid"). 결측은 키를 빼지 않고 null 로 둔다.
      - 값 모양은 분석 JSON 계약 v3 0절: 식별자는 소문자·하이픈 UUID 문자열, 날짜는 YYYY-MM-DD,
        시각은 YYYY-MM-DDTHH:MM:SSZ(UTC, 초 단위), 수량은 개(EA) 정수, raw 값은 소수 4자리 10진 문자열.
      - 식별자·시각을 uuid.UUID·datetime 으로 받지 않는 이유: 두 타입은 대문자 UUID·+09:00·마이크로초를 받아
        다른 모양으로 바꿔 내보낸다(11-04 확인). 엔진도 시각을 문자열 앞 10자리로 잘라 날짜를 비교한다(common.d).
"""


class ToolModel(BaseModel):
    """도구 입력·출력 모델의 바탕. 키 변환 없이 계약 키 그대로 주고받는다."""

    model_config = ConfigDict(extra="forbid")


class ToolName(StrEnum):
    """도구 이름 = AIR-001 '등록된 도구'의 기준 목록 (계약 2절, agentflow 4절 가칭을 정식 이름으로 씀)"""

    GET_DELAY_CONTEXT = "get_delay_context"  # T1
    GET_INVENTORY_SNAPSHOT = "get_inventory_snapshot"  # T2
    GET_INBOUND_SCHEDULE = "get_inbound_schedule"  # T3
    GET_SALES_HISTORY = "get_sales_history"  # T4
    GET_OPEN_ORDERS = "get_open_orders"  # T5
    CALCULATE_INVENTORY_IMPACT = "calculate_inventory_impact"  # T6
    IDENTIFY_AT_RISK_ORDERS = "identify_at_risk_orders"  # T7
    GET_OPERATING_RULES = "get_operating_rules"  # T8
    CHECK_OPTION_FEASIBILITY = "check_option_feasibility"  # T9
    CALCULATE_OPTION_OUTCOMES = "calculate_option_outcomes"  # T10
    COMPARE_OPTIONS = "compare_options"  # T11
    VALIDATE_AGENT_OUTPUT = "validate_agent_output"  # T12


TOOL_NUMBERS: dict[ToolName, str] = {
    name: f"T{i}" for i, name in enumerate(ToolName, start=1)
}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _require_date(value: object) -> object:
    """date 객체 또는 YYYY-MM-DD 문자열만 받는다. Pydantic 기본은 정수(유닉스 시각)·datetime 도 날짜로 바꾼다."""
    if isinstance(value, str):
        if not _ISO_DATE.match(value):
            raise ValueError("날짜는 YYYY-MM-DD 형식이어야 합니다")
        return value
    if type(value) is not date:
        raise ValueError("날짜는 date 또는 YYYY-MM-DD 문자열이어야 합니다")
    return value


ToolDate = Annotated[date, BeforeValidator(_require_date)]


def _require_unique[T](values: list[T]) -> list[T]:
    """같은 값이 두 번 들어 있으면 거부한다. 엔진은 이 목록들을 집합에서 만든다(core.impact_sets·delay_induced_skus 등)."""
    duplicated = sorted(str(v) for v, n in Counter(values).items() if n > 1)
    if duplicated:
        raise ValueError(f"중복 값이 있습니다: {duplicated}")
    return values


# 코드·날짜 값 배열(SKU 코드 집합, 행사 이름, 실제 수요를 아는 날). 중복 없음은 모델이, 정렬은 도구가 맞춘다(계약 1절)
UniqueCodes = Annotated[list[StrictStr], AfterValidator(_require_unique)]
UniqueDates = Annotated[list[ToolDate], AfterValidator(_require_unique)]
ToolId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    ),
]
UtcTimestamp = Annotated[
    str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
]
# 음수가 없는 raw 값(부족·일별 부족)과 음수가 될 수 있는 raw 값(min_gap = 가용 − 누적 수요)
RawQuantity = Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]{4}$")]
SignedRawQuantity = Annotated[str, StringConstraints(pattern=r"^-?[0-9]+\.[0-9]{4}$")]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

# 창고 권역 (warehouses_ck2). 서버 열거형 CustomerRegion 은 고객 권역(WEST·EAST)이라 KR 이 없어 따로 둔다.
# 운송 수단·단계는 app/modules/shipment/infrastructure/shipment_enums.py 의 열거형을 쓴다.
RegionCode = Literal["KR", "WEST", "EAST"]


def end_of_day(day: date) -> UtcTimestamp:
    """날짜를 그날 끝 시각(T23:59:59Z)으로 바꾼다. 엔진 storage.py 가 배송일을 저장 시각으로 바꾸는 방식과 같다."""
    return f"{day.isoformat()}T23:59:59Z"
