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
      - 11-06 발견: 엔진 explain/textcheck.tokens 가 구분자 없는 SKU 코드 안의 숫자를 수치로 세어(CREAM20 → 20)
        9건 중 8건이 VALUE_MISMATCH@risk_summary#s2 로 실패했다. 엔진 수정판(glowship-plan f18f86d, 11-11)은 이런 코드도
        코드 토큰으로 빼내므로 9건 모두 통과한다. 수정판이 아닌 엔진은 해시 목록 대조에서 먼저 막힌다.
"""

RUN_ID = "5e05f1a0-2026-4c00-9b00-00000000058a"
NOW = datetime(2026, 3, 10, 0, 5, tzinfo=UTC)


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


@pytest.mark.parametrize("key", KEYS)
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


def test_engine_tokenizer_takes_codes_without_separator_as_code_tokens(glow50):
    """엔진 수정판 고정(11-11): 구분자 없는 코드(CREAM20) 안의 숫자는 정수로 세지 않고 코드 토큰으로 뺀다.
    glow50 을 받아 엔진 경로(GLOWSHIP_ENGINE_DIR)가 없으면 이 폴더의 다른 시험처럼 건너뛴다(#66 리뷰)."""
    tokens = load_engine_module("explain.textcheck").tokens
    assert tokens("SKU별 지연 증분은 CREAM20 100개입니다") == {
        "codes": ["CREAM20"],
        "dates": [],
        "numbers": [100],
    }
    assert tokens("SKU별 지연 증분은 X 120개, Y 0개입니다")["numbers"] == [120, 0]
    # '-'·'_' 로 이어진 코드(실제 DB 모양)는 수정 전과 같이 코드 토큰
    assert tokens("SKU별 지연 증분은 SKU-005 31개입니다") == {
        "codes": ["SKU-005"],
        "dates": [],
        "numbers": [31],
    }
