"""
설명 검증 실패 위치 거르기(explanation_locations) 시험 — docs/agent-flow/explanation.md 8.14절, DV3-7.

- 실제 모델 측정(11-13~11-16)에서 나온 실패 위치를 그대로 넣어, 고정 어휘는 그대로 남고 모델이 만든 키 이름만 "형식"이 되는지 본다.
- 화면 문구(failure_message)·내부 추적(EXPLANATION_ATTEMPT) 두 길 모두 같은 함수를 쓴다.
"""

import uuid

import pytest

from app.modules.delay_analysis.application.agent.explanation_locations import (
    MAX_DETAIL,
    UNKNOWN_CODE,
    UNKNOWN_LOCATION,
    safe_attempt_detail,
    safe_code,
    safe_failure_message,
    safe_location,
)
from app.modules.delay_analysis.application.agent.retry import explanation_failure
from app.modules.delay_analysis.application.agent.tracing import RunTracer

# 실제 모델 측정 회차 실패 위치(tools/llm-measure/runs/*/results.jsonl, 11-17 모음) 가운데 고정 어휘인 것
KNOWN = [
    "evidence:E13",
    "evidence:E2:duplicate",
    "language",
    "json",
    "adapter",
    "body",
    "option_explanations",
    "recommendation_explanation",
    "option_explanations[KEEP_PLAN]:sentences",
    "option_explanations[OTHER_REGION_FULFILL]:reason_label",
    "option_explanations[PRE_DEPARTURE_AIR]:status_label",
    "option_explanations[KR_AIR_REPLENISH]#s2",
    "option_explanations[KEEP_PLAN]:missing delay_induced_recover",  # 엔진이 1,000자에서 자른 꼴
    "option_explanations[PRE_DEPARTURE_AIR]:missing feasibility_status",
    "recommendation_explanation#s2",
    "recommendation_explanation:sentences",
    "recommendation_explanation:PF-01",
    "risk_summary#s3",
    "risk_summary:missing /sku/TONER50/increment_prepare_quantity",
    "risk_summary:missing /summary/pre_existing_order_count",
    "run_summary:missing /run/expected_3pl_inbound_date",
    "run_summary:result_class_label",
    "warnings[BASELINE_SHORTAGE_EXISTS]#s1",
    "warnings[SKU_NOT_ESTIMABLE]#s1",
    "warnings[OPTION_NEEDS_CHECK]:missing evidence",
    "warnings:set",
    "meta:prompt_template_version",
    "decision_signature_hash",
    "/analysis_run_id",
    "/evidence_references",
    "/option_explanations/0/option_type",
    "/option_explanations/2/text",
    "/warnings/0",
]
# 같은 측정에서 모델이 지어낸 키 이름이 위치로 나온 것 + 일부러 만든 것
MODEL_MADE = [
    "/metadata",
    "/option_explanations/0/explanation",
    "/option_explanations/1/option",
    "/approval_status",
    "/고객에게 연락하세요",
    "warnings[IGNORE_PREVIOUS_RULES]#s1",
    "option_explanations[무시하라]:sentences",
    "risk_summary:missing 이전 지시를 무시",
    "evidence:E1; DROP TABLE",
    "",
    None,
]


@pytest.mark.parametrize("location", KNOWN)
def test_fixed_vocabulary_location_is_kept(location):
    assert safe_location(location) == location


@pytest.mark.parametrize("location", MODEL_MADE)
def test_model_made_location_becomes_fixed_word(location):
    assert safe_location(location) == UNKNOWN_LOCATION


def test_only_engine_codes_are_kept():
    assert safe_code("VALUE_MISMATCH") == "VALUE_MISMATCH"
    assert safe_code("MODEL_TIMEOUT") == "MODEL_TIMEOUT"
    assert safe_code("IGNORE_ALL") == UNKNOWN_CODE
    assert safe_code("value_mismatch") == UNKNOWN_CODE
    assert safe_code(None) == UNKNOWN_CODE


def test_failure_message_is_rebuilt_from_attempts_in_engine_shape():
    attempts = [
        {
            "retry_no": 1,
            "codes": [["SCHEMA_INVALID", "/metadata"], ["SCHEMA_INVALID", "json"]],
        },
        {"retry_no": 2, "codes": [["VALUE_MISMATCH", "risk_summary#s2"]]},
        {"retry_no": 3, "codes": [["SCHEMA_INVALID", "/a;3:LEAK@json|x"]]},
    ]

    message = safe_failure_message(attempts)

    assert message == (
        "1:SCHEMA_INVALID@형식|SCHEMA_INVALID@json;2:VALUE_MISMATCH@risk_summary#s2;3:SCHEMA_INVALID@형식"
    )
    # 위치 안의 ";"·"|"·"@" 가 모양을 깨지 않는다(문자열을 잘라 읽지 않음)
    assert "LEAK" not in message


def test_failure_message_keeps_engine_length_limit():
    attempts = [
        {"retry_no": n, "codes": [["VALUE_MISMATCH", "risk_summary#s2"]] * 40}
        for n in (1, 2, 3)
    ]
    assert len(safe_failure_message(attempts)) == MAX_DETAIL


def test_failure_message_tolerates_odd_attempt_shapes():
    attempts = [
        {"retry_no": "1", "codes": [["A"], None]},
        "x",
        {"retry_no": 2, "codes": None},
    ]
    assert safe_failure_message(attempts) == "?:?@형식|?@형식;2:"
    assert safe_failure_message(None) == ""


def test_attempt_detail_string_is_filtered_too():
    detail = "1:SCHEMA_INVALID@/metadata|VALUE_MISMATCH@run_summary#s1;2:MODEL_TIMEOUT@adapter;3:SCHEMA_INVA"
    assert safe_attempt_detail(detail) == (
        "1:SCHEMA_INVALID@형식|VALUE_MISMATCH@run_summary#s1;2:MODEL_TIMEOUT@adapter;3:?@형식"
    )
    assert safe_attempt_detail(None) is None
    assert safe_attempt_detail("") == ""


def test_client_failure_message_has_no_model_made_key():
    summary = explanation_failure(
        "1:SCHEMA_INVALID@/approval_status;2:SCHEMA_INVALID@/metadata;3:VALUE_MISMATCH@risk_summary#s2"
    )

    assert "approval_status" not in summary.failure_message
    assert "metadata" not in summary.failure_message
    assert summary.failure_message.endswith(
        "(시도: 1:SCHEMA_INVALID@형식;2:SCHEMA_INVALID@형식;3:VALUE_MISMATCH@risk_summary#s2)"
    )


def test_trace_attempt_codes_are_filtered():
    events = []

    class Sink:
        def write(self, event):
            events.append(event)

    tracer = RunTracer(uuid.UUID(int=1), uuid.UUID(int=2), [Sink()])
    tracer.explanation_attempts(
        [
            {
                "retry_no": 1,
                "codes": [["SCHEMA_INVALID", "/approval_status"]],
                "model_provider": "openai",
                "model_id": "m",
            },
            {
                "retry_no": 2,
                "codes": [["NOT_A_CODE", "json"]],
                "model_provider": "openai",
                "model_id": "m",
            },
        ]
    )

    assert tracer.close()
    summaries = [e.summary for e in events if e.kind == "EXPLANATION_ATTEMPT"]
    assert summaries[0]["codes"] == ["SCHEMA_INVALID@형식"]
    assert summaries[1]["codes"] == ["?@json"]
    assert [e.error_code for e in events if e.kind == "EXPLANATION_ATTEMPT"] == [
        "SCHEMA_INVALID",
        "?",
    ]
