import copy
import re
import uuid

import pytest
import test_evaluation_graph as G
from real_path_support import fix_explanation_canary, require_engine
from sqlalchemy import text

from app.core.security.auth import CurrentUser
from app.modules.delay_analysis.application.agent.retry import (
    EXPLANATION_GENERATION_FAILED,
)
from app.modules.delay_analysis.application.analysis_run_detail_service import (
    AnalysisRunDetailService,
)
from app.modules.delay_analysis.application.result_save.result_rows import (
    build_result_rows,
)
from app.modules.delay_analysis.application.run_start.outcome_save import (
    analysis_result_from_state,
)
from app.modules.delay_analysis.infrastructure.repository.analysis_run_detail_repository import (
    AnalysisRunDetailRepository,
)
from app.modules.delay_analysis.infrastructure.repository.delay_event_read_repository import (
    DelayEventReadRepository,
)
from app.modules.delay_analysis.presentation.analysis_run_schemas import (
    AnalysisRunDetailResponse,
)

"""
    NFR-004 근거성 — 시스템이 제시한 대응안 추천의 계산 결과·적용 기준 연결률 100% (검증 방법: 추천 결과와 실행 기록 검사)
      - "제시한 추천" = 설명 검증을 통과한 실행의 추천(명세 v6.2 4.5 "추천 표시는 설명 검증을 통과한 실행의 1순위 FEASIBLE
        대응안 1개다", 설명 실패 실행은 "초안·최종 선택 불가"). 추천 하나가 아래 고리를 모두 채우면 "연결됨".
          L1 추천: isRecommended 가 정확히 1개, 그 대응안이 순위 1·FEASIBLE
          L2 계산 결과: 재고 영향 행 1개 이상, 추천 대응안의 계산 칸(필요·확보·감소·예상 도착·회복 4종·SKU별 결과)이
             모두 채워짐, 주문별 결과 수 = 위험 주문 상세 수
          L3 적용 기준: 입력 순번 1 이상·기준일 D·운영 기준 묶음과 판·계산 계약 판·배정 규칙 판·수집 기준 시각, 데이터 버전 1개 이상
          L4 설명 근거: 설명이 이 실행·이 판정 서명을 가리키고, 근거 참조의 추천 대응안 = 추천, 추천 문장의 [E#] 가 모두 근거 참조에 있음
      - C1 개발 DB: 실행 전부를 실행 상세 응답(API 17 과 같은 길)으로 만들어 센다. 분모는 응답과 따로 쓴 SQL 로 센다
        (응답이 추천을 떨어뜨리면 분모가 어긋나 실패).
      - C2 평가 시나리오 9건: 서버가 지금 만드는 저장 행에서 L1·L2·L4 를 센다(시나리오는 DB 에 없어 입력 기준 L3 는 C1 만).
      - C3 검사기 음성: 고리 하나를 끊은 사본은 "연결 안 됨"으로 센다.
      - 설명 실패 실행(EXPLANATION_GENERATION_FAILED)은 추천 행이 저장·응답되지만(DDL response_options_ck16, 데이터 팀
        정량 정답표 v2 E2 v1) 제시한 추천이 아니라 분모 밖이다. 그 실행은 지금 모습을 따로 고정한다: L1·L2 는 채워지고
        적용 기준 중 기준일 D 만 비어 있다(데이터 팀 JSON 계약 v3 2절 "FAILED 는 의존 범위 NULL", 서버는 기준일을 거기서 읽음).
        이 모습을 그대로 둘지는 설계·화면에서 정할 일이고, 규칙이 바뀌면 이 시험이 깨져 알린다.
"""

CALCULATION_FIELDS = (
    "requiredQuantity",
    "securableQuantity",
    "reductionCommittedTotal",
    "expectedDeliveryDate",
    "fullyRecoveredOrders",
    "delayInducedRecoveredOrders",
    "itemSecuredBlockedOrders",
    "notRecoveredOrders",
    "skuResults",
)
BASIS_FIELDS = (
    "baseDate",
    "operatingRuleSetId",
    "operatingRuleVersionNo",
    "calculationContractVersion",
    "allocationRuleVersion",
    "dataBasisAt",
)
_MARK = re.compile(r"\[(E\d+)\]")


def broken_links(view: dict, *, with_explanation: bool = True) -> list[str]:
    """
    끊긴 고리 목록(빈 목록 = 연결됨). view 는 응답·저장 행에서 만든 같은 모양:
      options[]: optionType·recommendationRank·isRecommended·feasibilityStatus·outcomes(수)·계산 칸
      impacts·riskLines(수), basis(입력 기준, 모르면 None = L3 건너뜀), explanation·explanationStatus·runId·signatureSha
    """
    broken: list[str] = []
    recommended = [o for o in view["options"] if o["isRecommended"]]
    if len(recommended) != 1:
        return [f"L1 추천 {len(recommended)}개"]
    rec = recommended[0]
    if rec["recommendationRank"] != 1 or rec["feasibilityStatus"] != "FEASIBLE":
        broken.append("L1 순위 1·FEASIBLE 아님")

    if view["impacts"] < 1:
        broken.append("L2 재고 영향 없음")
    broken += [f"L2 {f} 없음" for f in CALCULATION_FIELDS if rec[f] is None]
    if rec["outcomes"] != view["riskLines"]:
        broken.append("L2 주문별 결과 수 ≠ 위험 주문 상세 수")

    basis = view["basis"]
    if basis is not None:
        if not (basis["inputSeq"] or 0) >= 1:
            broken.append("L3 inputSeq 없음")
        broken += [f"L3 {f} 없음" for f in BASIS_FIELDS if basis[f] is None]
        if not basis["dataVersions"]:
            broken.append("L3 dataVersions 없음")

    if with_explanation:
        doc = view["explanation"]
        if view["explanationStatus"] != "PASSED" or doc is None:
            return [*broken, "L4 설명 없음"]
        if doc["analysis_run_id"] != view["runId"]:
            broken.append("L4 설명의 실행 다름")
        if doc["decision_signature_hash"] != view["signatureSha"]:
            broken.append("L4 판정 서명 다름")
        refs = {r["id"]: r for r in doc["evidence_references"]}
        if not any(
            r["path"] == "/recommendation/option_type"
            and r["value"] == rec["optionType"]
            for r in refs.values()
        ):
            broken.append("L4 근거 참조의 추천 다름")
        marks = _MARK.findall(doc["recommendation_explanation"])
        if not marks or not set(marks) <= set(refs):
            broken.append("L4 추천 문장 근거 표지 없음")
    return broken


# --- C1 개발 DB: 실행 상세 응답 ----------------------------------------------------------------

_STORED = text(
    """
    SELECT c.company_code, r.run_code, r.analysis_run_id, r.company_id,
           r.explanation_validation_status, r.decision_signature_sha256, r.failure_code,
           EXISTS (SELECT 1 FROM response_options o
                    WHERE o.analysis_run_id = r.analysis_run_id AND o.is_recommended) AS has_recommendation
      FROM analysis_runs r JOIN companies c USING (company_id)
     ORDER BY c.company_code, r.run_code
    """
)


async def _views(db) -> list[tuple[dict, dict]]:
    """(SQL 로 읽은 저장값, 응답으로 만든 view) — 실행마다."""
    stored = [dict(r._mapping) for r in (await db.execute(_STORED)).all()]
    if not stored:
        pytest.skip("분석 실행이 없는 DB")
    service = AnalysisRunDetailService(
        db=db,
        repo=AnalysisRunDetailRepository(db),
        delay_repo=DelayEventReadRepository(db),
    )
    out = []
    for s in stored:
        detail = await service.get_analysis_run(
            actor=CurrentUser(
                user_id=uuid.uuid7(),
                role_code="CLIENT_MEMBER",
                company_id=s["company_id"],
            ),
            run_id=s["analysis_run_id"],
        )
        resp = AnalysisRunDetailResponse.of(detail).model_dump(
            mode="json", by_alias=True
        )
        view = {
            "options": [
                {**o, "outcomes": len(o.get("outcomes") or [])}
                for o in resp["responseOptions"]
            ],
            "impacts": len(resp["inventoryImpacts"]),
            "riskLines": sum(len(a["lines"]) for a in resp["atRiskOrders"]),
            "basis": resp["inputBasis"],
            "explanation": resp["explanation"],
            "explanationStatus": resp["explanationValidationStatus"],
            "runId": resp["runId"],
            "signatureSha": s["decision_signature_sha256"],
            "failureCode": resp["failureCode"],
        }
        out.append((s, view))
    return out


def _code(stored: dict) -> str:
    return f"{stored['company_code']} {stored['run_code']}"


def _recommends(view: dict) -> bool:
    return any(o["isRecommended"] for o in view["options"])


async def test_every_presented_recommendation_is_linked(db_session):
    """C1: 설명을 통과한 실행의 추천은 계산 결과·적용 기준·설명 근거에 모두 연결된다(연결률 100%)."""
    runs = await _views(db_session)
    presented = [
        (s, v)
        for s, v in runs
        if s["has_recommendation"] and s["explanation_validation_status"] == "PASSED"
    ]
    shown = [
        (s, v) for s, v in runs if v["explanationStatus"] == "PASSED" and _recommends(v)
    ]

    assert presented, "제시한 추천이 하나도 없으면 연결률을 셀 수 없다"
    assert [_code(s) for s, _ in shown] == [_code(s) for s, _ in presented]
    assert {_code(s): broken_links(v) for s, v in presented if broken_links(v)} == {}


async def test_runs_without_presented_recommendation(db_session):
    """
    C1: 제시한 추천이 없는 실행. 추천이 없는 실행은 응답에도 없고, 설명 실패 실행만 — 그리고 모두 — 추천 행이 남는다.
    설명 실패 실행의 추천은 계산 결과(L1·L2)는 채워져 있고 적용 기준 중 기준일 D 만 비어 있다(지금 모습 고정).
    설명 실패 실행이 없는 DB 에서는 그 고정만 건너뛴다(자료가 없는 것과 규칙이 바뀐 것을 가른다).
    """
    rest = [
        (s, v)
        for s, v in await _views(db_session)
        if not (
            s["has_recommendation"] and s["explanation_validation_status"] == "PASSED"
        )
    ]

    without = [(s, v) for s, v in rest if not s["has_recommendation"]]
    failed = [(s, v) for s, v in rest if s["has_recommendation"]]
    explanation_failed = [
        _code(s) for s, _ in rest if s["failure_code"] == EXPLANATION_GENERATION_FAILED
    ]
    assert [_code(s) for s, v in without if _recommends(v)] == []
    assert [_code(s) for s, _ in failed] == explanation_failed
    if not failed:
        pytest.skip("설명 실패 실행이 없는 DB 라 그 실행의 추천 고정을 건너뜀")
    assert {
        _code(s): (v["failureCode"], broken_links(v, with_explanation=False))
        for s, v in failed
    } == {
        _code(s): (EXPLANATION_GENERATION_FAILED, ["L3 baseDate 없음"])
        for s, _ in failed
    }


# --- C2 평가 시나리오 9건: 서버가 지금 만드는 저장 행 --------------------------------------------


def _row_view(rows) -> dict:
    """저장 행 → view. 저장 칸 이름을 응답 이름으로 옮긴다(L3 입력 기준은 시나리오에 없어 None)."""
    camel = {
        "required_quantity": "requiredQuantity",
        "securable_quantity": "securableQuantity",
        "reduction_committed_total": "reductionCommittedTotal",
        "expected_delivery_date": "expectedDeliveryDate",
        "fully_recovered_orders": "fullyRecoveredOrders",
        "delay_induced_recovered_orders": "delayInducedRecoveredOrders",
        "item_secured_blocked_orders": "itemSecuredBlockedOrders",
        "not_recovered_orders": "notRecoveredOrders",
        "sku_results": "skuResults",
        "option_type": "optionType",
        "recommendation_rank": "recommendationRank",
        "is_recommended": "isRecommended",
        "feasibility_status": "feasibilityStatus",
    }
    options = []
    for row in rows.response_options:
        option = {camel[k]: v for k, v in row.items() if k in camel}
        option["outcomes"] = sum(
            1
            for x in rows.option_order_outcomes
            if x["response_option_id"] == row["response_option_id"]
        )
        options.append(option)
    values = rows.run_values
    return {
        "options": options,
        "impacts": len(rows.inventory_impact_results),
        "riskLines": len(rows.at_risk_orders),
        "basis": None,
        "explanation": values.get("final_explanation_json"),
        "explanationStatus": values.get("explanation_validation_status"),
        "runId": str(G.RUN_ID),
        "signatureSha": values["decision_signature_sha256"],
    }


@pytest.mark.parametrize("key", G.KEYS)
async def test_scenario_recommendation_is_linked(scenario, key, glow50, monkeypatch):
    """C2: 서버가 지금 만드는 저장 행에서도 추천이 계산 결과·설명 근거에 연결된다."""
    require_engine()
    fix_explanation_canary(monkeypatch)
    state = await G._state(scenario, key)
    rows = build_result_rows(
        analysis_result_from_state(state),
        company_id=G.COMPANY_ID,
        analysis_run_id=G.RUN_ID,
    )

    view = _row_view(rows)

    assert broken_links(view) == []


# --- C3 검사기 음성: 고리 하나를 끊은 사본 ------------------------------------------------------


def _linked_view() -> dict:
    option = {
        "optionType": "KR_AIR_REPLENISH",
        "recommendationRank": 1,
        "isRecommended": True,
        "feasibilityStatus": "FEASIBLE",
        "outcomes": 2,
        **dict.fromkeys(CALCULATION_FIELDS, 1),
    }
    keep = {
        **option,
        "optionType": "KEEP_PLAN",
        "recommendationRank": 2,
        "isRecommended": False,
    }
    run_id = str(uuid.UUID(int=55))
    return {
        "options": [keep, option],
        "impacts": 3,
        "riskLines": 2,
        "basis": {
            "inputSeq": 10,
            **dict.fromkeys(BASIS_FIELDS, "값"),
            "dataVersions": [{"sourceDomain": "ORDER"}],
        },
        "explanation": {
            "analysis_run_id": run_id,
            "decision_signature_hash": "ab" * 32,
            "recommendation_explanation": "추천 대응안은 한국 창고 항공 보충입니다 [E19].",
            "evidence_references": [
                {
                    "id": "E19",
                    "path": "/recommendation/option_type",
                    "value": "KR_AIR_REPLENISH",
                }
            ],
        },
        "explanationStatus": "PASSED",
        "runId": run_id,
        "signatureSha": "ab" * 32,
    }


def _rec(v):
    return v["options"][1]


BREAKS = {
    "추천 2개": (lambda v: v["options"][0].update(isRecommended=True), "L1 추천 2개"),
    "추천 없음": (lambda v: _rec(v).update(isRecommended=False), "L1 추천 0개"),
    "추천이 2순위": (
        lambda v: _rec(v).update(recommendationRank=2),
        "L1 순위 1·FEASIBLE 아님",
    ),
    "추천이 FEASIBLE 아님": (
        lambda v: _rec(v).update(feasibilityStatus="NEEDS_CHECK"),
        "L1 순위 1·FEASIBLE 아님",
    ),
    "재고 영향 없음": (lambda v: v.update(impacts=0), "L2 재고 영향 없음"),
    "확정 감소 비움": (
        lambda v: _rec(v).update(reductionCommittedTotal=None),
        "L2 reductionCommittedTotal 없음",
    ),
    "SKU별 결과 비움": (
        lambda v: _rec(v).update(skuResults=None),
        "L2 skuResults 없음",
    ),
    "주문별 결과 빠짐": (
        lambda v: _rec(v).update(outcomes=1),
        "L2 주문별 결과 수 ≠ 위험 주문 상세 수",
    ),
    "입력 순번 0": (lambda v: v["basis"].update(inputSeq=0), "L3 inputSeq 없음"),
    "기준일 비움": (lambda v: v["basis"].update(baseDate=None), "L3 baseDate 없음"),
    "운영 기준 비움": (
        lambda v: v["basis"].update(operatingRuleSetId=None),
        "L3 operatingRuleSetId 없음",
    ),
    "데이터 버전 없음": (
        lambda v: v["basis"].update(dataVersions=[]),
        "L3 dataVersions 없음",
    ),
    "설명 없음": (
        lambda v: v.update(explanation=None, explanationStatus="FAILED"),
        "L4 설명 없음",
    ),
    "다른 실행의 설명": (
        lambda v: v["explanation"].update(analysis_run_id=str(uuid.UUID(int=56))),
        "L4 설명의 실행 다름",
    ),
    "판정 서명 다름": (lambda v: v.update(signatureSha="cd" * 32), "L4 판정 서명 다름"),
    "근거 참조의 추천 다름": (
        lambda v: v["explanation"]["evidence_references"][0].update(value="KEEP_PLAN"),
        "L4 근거 참조의 추천 다름",
    ),
    "추천 문장 표지 없음": (
        lambda v: v["explanation"].update(recommendation_explanation="추천합니다."),
        "L4 추천 문장 근거 표지 없음",
    ),
    "추천 문장 표지가 근거에 없음": (
        lambda v: v["explanation"].update(
            recommendation_explanation="추천합니다 [E9]."
        ),
        "L4 추천 문장 근거 표지 없음",
    ),
}


def test_linked_view_has_no_broken_link():
    """C3 대조군: 고리를 모두 채운 사본은 연결됨."""
    assert broken_links(_linked_view()) == []


@pytest.mark.parametrize("name", list(BREAKS))
def test_checker_catches_each_broken_link(name):
    """C3: 고리 하나를 끊으면 그 고리가 끊겼다고 센다."""
    view = copy.deepcopy(_linked_view())
    cut, expected = BREAKS[name]
    cut(view)

    assert expected in broken_links(view)
