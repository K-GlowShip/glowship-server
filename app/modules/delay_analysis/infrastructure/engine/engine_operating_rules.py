from typing import Any

"""
    운영 기준 검사: 계산이 지원하지 않는 운영 기준은 분석 준비에서 보류 (GLOW-100, docs/analysis/engine-input.md 4절 8번)
      - 계산 계약 T14-v1 은 판매 기준 28일·달력일 리드타임만 계산한다(계약 9절 Q5·Q6).
        엔진은 운영 기준의 sales_basis_days 를 스냅샷에 담기만 하고 수요는 28일로 세며,
        리드타임은 day_basis 를 보지 않고 일수를 달력일로 쓴다. 다른 값이 들어오면 조용히 틀린 결과가 된다.
      - 그래서 엔진 스냅샷(snapshot.build)이 고른 운영 기준 버전과 리드타임 행을 보고,
        지원하지 않는 값이 있으면 엔진 계산(pipeline.analyze) 전에 보류 문구를 돌려준다(engine_input._build_and_analyze).
      - 이 실행의 계산이 읽는 행만 본다: 스냅샷의 운영 기준 버전, 그 버전에서 엔진이 고른 리드타임 7개 행.
        다른 창고·권역·버전의 행은 계산에 쓰이지 않으므로 막지 않는다.
      - 순수 함수다. 저장소·스냅샷을 읽기만 한다.
"""

SUPPORTED_SALES_BASIS_DAYS = 28
SUPPORTED_DAY_BASIS = "CALENDAR_DAY"
# 보류 코드. 엔진 snapshot.Hold 의 검증 오류 코드, 실패 요약 보류 코드(retry.HOLD_CODES)와 같은 값이다(계약 9절 Q6)
UNSUPPORTED_RULE_HOLD_CODE = "REQUIRED_DATA_INVALID"


def lead_time_rows(store, snap: dict[str, Any]) -> dict[str, dict[str, str] | None]:
    """엔진이 리드타임 7개를 고른 행(snapshot.build 의 lt). 열쇠 순서는 계약 3.6절 필드 순서다.

    고르는 규칙은 엔진과 같다: store.lead_days 는 조건에 맞는 첫 행, CUSTOMER_DELIVERY 는 반복에서 마지막으로 맞은 행."""
    cid, rid = snap["company_id"], snap["rule"]["operating_rule_set_id"]
    dest, donor, kr = snap["dest"], snap["donor"], snap["kr"]
    rows = [
        r for r in store.t["operating_lead_times"]
        if r["company_id"] == cid and r["operating_rule_set_id"] == rid
    ]  # fmt: skip

    def first(ltype: str, wh_id: str, to_region: str | None = None):
        return next(
            (r for r in rows if r["lead_time_type"] == ltype and r["warehouse_id"] == wh_id
             and (to_region is None or r["to_region_code"] == to_region)),
            None,
        )  # fmt: skip

    def last_delivery(wh_id: str):
        found = None
        for r in rows:
            if (
                r["lead_time_type"] == "CUSTOMER_DELIVERY"
                and r["warehouse_id"] == wh_id
                and r["to_region_code"] == dest["region_code"]
            ):
                found = r
        return found

    return {
        "oh_dest": first("OUTBOUND_HANDLING", dest["warehouse_id"]),
        "cd_same": last_delivery(dest["warehouse_id"]),
        "oh_donor": first("OUTBOUND_HANDLING", donor["warehouse_id"]),
        "cd_cross": last_delivery(donor["warehouse_id"]),
        "oh_kr": first("OUTBOUND_HANDLING", kr["warehouse_id"]),
        "air_kr": first("AIR_TRANSIT", kr["warehouse_id"], dest["region_code"]),
        "inbound_dest": first("INBOUND_HANDLING", dest["warehouse_id"]),
    }


def _lead_time_label(row: dict[str, str], snap: dict[str, Any]) -> str:
    """보류 문구에 적는 리드타임 행: 종류, 창고 코드, (있으면) 도착 권역, 기준. 운영 기준 화면에서 고칠 행을 찾는 값이다."""
    code_of = {
        w["warehouse_id"]: w["warehouse_code"]
        for w in (snap["dest"], snap["donor"], snap["kr"])
    }
    label = f"{row['lead_time_type']} {code_of[row['warehouse_id']]}"
    if row["to_region_code"]:
        label += f"→{row['to_region_code']}"
    return f"{label} {row['day_basis']}"


def unsupported_operating_rules(store, snap: dict[str, Any]) -> str | None:
    """계산이 지원하지 않는 운영 기준 값이 있으면 보류 문구, 없으면 None.

    항목은 정해진 순서(판매 기준 일수 → 리드타임 7개, 계약 3.6절 순서)로 한 문구에 모두 적는다.
    운영 기준을 한 번에 고칠 수 있게 하려는 것이다. 없는 리드타임 행은 여기서 보지 않는다
    (도착 창고 출고 처리·고객 배송이 없으면 엔진이 보류, 나머지는 대응안이 MISSING_OPERATING_RULE)."""
    found = []
    if snap["basis_days"] != SUPPORTED_SALES_BASIS_DAYS:
        found.append(
            f"판매 기준 일수 {snap['basis_days']}(계산은 {SUPPORTED_SALES_BASIS_DAYS}일만 지원)"
        )
    not_calendar = [
        _lead_time_label(row, snap)
        for row in lead_time_rows(store, snap).values()
        if row is not None and row["day_basis"] != SUPPORTED_DAY_BASIS
    ]
    if not_calendar:
        found.append(
            f"리드타임 {'·'.join(not_calendar)}(계산은 {SUPPORTED_DAY_BASIS}만 지원)"
        )
    if not found:
        return None
    return f"운영 기준 버전 {snap['rule']['version_no']}: {', '.join(found)}"
