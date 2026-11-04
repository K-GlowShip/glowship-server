from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StringConstraints

from app.modules.delay_analysis.infrastructure.analysis_enums import (
    ImpactSource,
    SkuEvaluationStatus,
    SkuNotEstimableReason,
    SkuScope,
)

"""
    데이터 팀 계약으로 키가 고정된 저장 JSON 의 공용 타입 (분석 JSON 계약 v3 0절·1절)
      - 응답 스키마(presentation)와 에이전트 도구 입출력 모델(application/agent/tools)이 함께 쓴다.
        한쪽에만 두면 다른 계층이 거꾸로 불러오게 되므로, 코드값(analysis_enums)과 같은 계층에 둔다.
      - 키는 snake_case 그대로 주고받고(D20), 알 수 없는 키는 받지 않는다(extra="forbid").
      - 저장 JSON 의 정수는 StrictInt 로 받아 불리언·문자열을 정수로 바꿔 쓰지 않는다.
"""

# 소수 4자리 10진 문자열 (JSON 계약 0절 raw 값, 예: "2.8929"). 부족·감소량처럼 음수가 없는 값에 쓴다.
RawDecimal = Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]{4}$")]
# 저장 JSON 의 식별자: 소문자·하이픈 UUID 문자열 (JSON 계약 0절, explanation.v1 analysis_run_id).
# uuid.UUID 로 받으면 다른 표기도 받아들이고 출력 때 바꿔 버리므로, 저장된 문자열 그대로 둔다.
StoredUuid = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    ),
]


class StoredJsonModel(BaseModel):
    """데이터 팀 계약으로 키가 고정된 저장 JSON. 키 변환 없이 그대로 주고받는다."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


# --- sku_results.v1 (response_options.sku_results) ---------------------------------------------


class SkuResultDonor(StoredJsonModel):
    """OTHER_REGION_FULFILL 의 공여 창고 계산. 정수는 개(EA)."""

    source_warehouse_id: StoredUuid
    next_inbound_basis: Literal["N2"]
    next_inbound_date: date | None  # null 이면 NOT_ESTIMABLE
    on_hand: StrictInt
    reserved: StrictInt
    open_commitment_net: StrictInt
    safety_stock_quantity: StrictInt
    forecast_until_next_raw: RawDecimal | None
    transferable_raw: RawDecimal | None
    transferable_quantity: StrictInt | None
    order_layer_used_quantity: StrictInt | None
    forecast_channel_share: RawDecimal | None
    forecast_securable_quantity: StrictInt | None


class SkuResultItem(StoredJsonModel):
    """대응안 1개의 SKU별 효과. EVALUATED 가 아니어도 원값이 채워져 있을 수 있다(2026 H1 데이터)."""

    sku_id: StoredUuid
    sku_scope: SkuScope
    # v1·v2 엔진 산출물에는 키가 없다. 없으면 "기록 없음"으로 읽는다(JSON 계약 v3 0.1절).
    impact_source: ImpactSource | None = None
    evaluation_status: SkuEvaluationStatus
    not_estimable_reason: SkuNotEstimableReason | None
    raw_baseline_shortage: RawDecimal | None
    raw_increment_shortage: RawDecimal | None
    raw_reduction: RawDecimal | None
    raw_residual_increment: RawDecimal | None
    baseline_prepare_quantity: StrictInt | None
    increment_prepare_quantity: StrictInt | None
    reduction_committed_quantity: StrictInt | None
    residual_prepare_quantity: StrictInt | None
    supply_quantity: StrictInt | None
    supply_available_date: date | None
    donor: SkuResultDonor | None


class SkuResults(StoredJsonModel):
    schema_version: Literal["sku_results.v1"]
    calculation_contract: Literal["T14-v1"]
    unit: Literal["EA"]
    items: list[SkuResultItem]
