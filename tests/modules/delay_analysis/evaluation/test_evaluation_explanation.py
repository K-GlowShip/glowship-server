import asyncio
import json
from datetime import UTC, datetime

import pytest
from test_evaluation_t9_t11 import ANSWERS, KEYS

from app.modules.delay_analysis.application.agent.explanation import (
    ExplanationFailed,
    ExplanationPassed,
    generate_explanation,
)
from app.modules.delay_analysis.application.agent.tools.output_validation import (
    validate_agent_output,
)
from app.modules.delay_analysis.application.agent.tools.validation_schemas import (
    AgentOutputStatus,
    ValidateAgentOutputInput,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

"""
    READY 시나리오 9건의 AI 설명 생성(GLOW-127 generate_explanation, mock 모델)과 T12 재검증
      - 기대(정답표 흐름): 설명이 첫 시도에 통과하고, 추천 대응안·대응안 집합이 정답표와 같으며, T12 로 다시 검증해도 PASSED.
      - 11-06 발견(엔진 몫): 엔진 explain/textcheck.tokens 가 SKU 코드 안의 숫자를 수치로 센다(CREAM20 → 20).
        모의 모델이 쓰는 "SKU별 지연 증분은 CREAM20 100개입니다" 문장은 인용 값 [100] 과 토큰 [20, 100] 이 달라
        VALUE_MISMATCH@risk_summary#s2 로 3번 모두 실패한다. 코드에 숫자가 없는 GS-05(X·Y)만 통과.
        실제 DB SKU 코드도 전부 숫자를 품고 있어(SKU-001 …) 고치지 않으면 운영에서 설명이 늘 실패한다.
        → 통과 기대 시험은 strict xfail 로 두고(엔진이 고쳐지면 XPASS 로 실패해 표시를 지우라고 알림),
          지금 동작(3번 실패 → #28 explanation_failure 모양)은 따로 고정한다.
"""

RUN_ID = "5e05f1a0-2026-4c00-9b00-00000000058a"
NOW = datetime(2026, 3, 10, 0, 5, tzinfo=UTC)
ENGINE_TOKENIZER_NOTE = (
    "엔진 textcheck.tokens 가 SKU 코드 안의 숫자를 수치로 세어 모의 설명이 risk_summary#s2 에서 "
    "VALUE_MISMATCH 로 실패함(11-06, 엔진 몫). 고쳐지면 이 표시를 지운다"
)
DIGIT_CODE_KEYS = [k for k in KEYS if k != "GS-05"]


@pytest.fixture(scope="module")
def fixed_canary():
    """엔진 canary(secrets.token_hex)가 우연히 전화번호 패턴에 맞는 흔들림을 막는다(test_explanation.py 와 같음)."""
    mp = pytest.MonkeyPatch()
    mp.setattr(
        load_engine_module("explain.generator").secrets,
        "token_hex",
        lambda n: "ab" * n,
    )
    yield
    mp.undo()


def _generate(key, snap, analysis):
    return asyncio.run(
        generate_explanation(
            snap=snap,
            analysis=analysis,
            analysis_run_id=RUN_ID,
            shipment_code=f"SHIP-{key}",
            now=NOW,
        )
    )


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(
            k, marks=pytest.mark.xfail(strict=True, reason=ENGINE_TOKENIZER_NOTE)
        )
        if k in DIGIT_CODE_KEYS
        else k
        for k in KEYS
    ],
)
def test_ready_scenario_gets_passed_explanation(scenario, fixed_canary, key):
    snap, _replay, analysis = scenario(key)
    result = _generate(key, snap, analysis)
    assert isinstance(result, ExplanationPassed), (
        result.failure_summary() if isinstance(result, ExplanationFailed) else result
    )
    assert result.attempts[-1]["retry_no"] == 1
    doc = result.final_explanation_json
    recommended = ANSWERS[key]["summary"]["recommended"]
    assert any(
        r["path"] == "/recommendation/option_type" and r["value"] == recommended
        for r in doc["evidence_references"]
    )
    assert [o["option_type"] for o in doc["option_explanations"]] == list(
        ANSWERS[key]["t9"]
    )
    check = validate_agent_output(
        ValidateAgentOutputInput(
            input_document=result.input_document,
            output=json.dumps(doc, ensure_ascii=False),
            expected_metadata=result.expected_metadata(),
            canary=None,
        )
    )
    assert check.status is AgentOutputStatus.PASSED
    assert check.errors == []


def test_engine_tokenizer_counts_digits_inside_sku_codes():
    """원인 고정. 이 시험이 실패하면 엔진이 고쳐진 것이니 위 xfail 과 아래 실패 고정 시험을 지운다."""
    tokens = load_engine_module("explain.textcheck").tokens
    assert tokens("SKU별 지연 증분은 CREAM20 100개입니다")["numbers"] == [20, 100]
    assert tokens("SKU별 지연 증분은 X 120개, Y 0개입니다")["numbers"] == [120, 0]


@pytest.mark.parametrize("key", DIGIT_CODE_KEYS)
def test_digit_code_scenarios_end_as_explanation_failure(scenario, fixed_canary, key):
    """지금 엔진: 3번 모두 VALUE_MISMATCH@risk_summary#s2 → 서버는 #28 explanation_failure 모양으로 끝낸다."""
    snap, _replay, analysis = scenario(key)
    result = _generate(key, snap, analysis)
    assert isinstance(result, ExplanationFailed)
    assert [a["codes"] for a in result.attempts] == [
        [["VALUE_MISMATCH", "risk_summary#s2"]]
    ] * 3
    summary = result.failure_summary()
    assert (
        summary.status.value,
        summary.failed_step.value,
        summary.failure_code,
        summary.attempt_count,
        summary.retry_exhausted,
    ) == ("FAILED", "VALIDATING_OUTPUT", "EXPLANATION_GENERATION_FAILED", 3, True)
