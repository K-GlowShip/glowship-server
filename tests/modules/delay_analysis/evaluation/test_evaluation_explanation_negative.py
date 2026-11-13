import copy
import json
import re

import pytest
import test_evaluation_explanation as E
from real_path_support import fix_explanation_canary, require_engine
from test_evaluation_t9_t11 import KEYS

from app.modules.delay_analysis.application.agent.explanation import (
    ExplanationPassed,
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
    NFR-003 설명 수치 오류 0 — 평가 시나리오 9건의 음성 짝
      - test_evaluation_explanation.py 는 9건이 "통과한다"만 본다. 그래서 서버 T12(validate_agent_output)가 엔진 검증기의
        결함을 버리도록 바꿔도(늘 통과) 그 시험은 그대로 통과했다. 이 파일은 같은 9건에서 통과한 설명을 한 군데만
        틀리게 바꿔 T12 가 떨어뜨리는지 본다.
      - N1 본문 숫자: 설명 문장의 수량 하나를 +1 → RETRY·VALUE_MISMATCH
      - N2 근거 값: 근거 참조(evidence_references) 하나의 값을 바꿈 → RETRY·VALUE_MISMATCH
      - N3 헛돌지 않음: 바꾼 문서가 원래와 실제로 다르고, 바꾸지 않은 문서는 PASSED(대조군)
      - 9건 = GS-06(보류, 설명 없음)을 뺀 READY 시나리오. 수동 검토 기록은 이 시험으로 갈음하지 않는다.
"""

# 숫자 앞뒤가 글자·숫자·'-'·'_'·'.' 가 아닌 정수(코드 CREAM20·날짜 2026-03-10·소수 안의 숫자는 건드리지 않음)
_INT = r"(?<![0-9A-Za-z_.\-]){n}(?![0-9A-Za-z_.\-])"


@pytest.fixture(autouse=True)
def _engine_and_canary(glow50, monkeypatch):
    require_engine()
    fix_explanation_canary(monkeypatch)


def _passed(scenario, key) -> ExplanationPassed:
    snap, _replay, analysis = scenario(key)
    result = E._generate(key, snap, analysis)
    assert isinstance(result, ExplanationPassed)
    return result


def _check(result: ExplanationPassed, doc: dict):
    return validate_agent_output(
        ValidateAgentOutputInput(
            input_document=result.input_document,
            output=json.dumps(doc, ensure_ascii=False),
            expected_metadata=result.expected_metadata(),
            canary=None,
        )
    )


def _sentences(doc: dict):
    """(경로, 글자) — 설명 문장 칸 전부(explanation.v1)."""
    for name in ("run_summary", "risk_summary", "recommendation_explanation"):
        yield (name,), doc[name]
    for group in ("option_explanations", "warnings"):
        for i, item in enumerate(doc[group]):
            yield (group, i, "text"), item["text"]


def _with_number_bumped(doc: dict) -> tuple[dict, tuple, int]:
    """
    엔진 토크나이저가 수치로 세는 정수 중 가장 큰 것(개수 1 보다 수량 100 처럼 계산 값일 가능성이 큼) 하나를 +1 한 사본.
    """
    tokens = load_engine_module("explain.textcheck").tokens
    found = [
        (n, path, text)
        for path, text in _sentences(doc)
        for n in tokens(text)["numbers"]
        if re.search(_INT.format(n=n), text)
    ]
    assert found, "바꿀 수치를 찾지 못했다 — 시험이 헛돈다"
    n, path, text = max(found, key=lambda f: f[0])
    changed = copy.deepcopy(doc)
    target = changed
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = re.sub(_INT.format(n=n), str(n + 1), text, count=1)
    return changed, path, n


def _codes(check) -> set[str]:
    return {e.code for e in check.errors}


@pytest.mark.parametrize("key", KEYS)
def test_unchanged_explanation_passes(scenario, key):
    """N3 대조군: 바꾸지 않은 설명은 T12 를 통과한다(아래 실패가 장치 탓이 아님)."""
    result = _passed(scenario, key)
    check = _check(result, result.final_explanation_json)
    assert check.status is AgentOutputStatus.PASSED, check.errors


@pytest.mark.parametrize("key", KEYS)
def test_changed_number_in_sentence_is_caught(scenario, key):
    """N1: 문장 속 수량 하나를 +1 하면 RETRY·VALUE_MISMATCH."""
    result = _passed(scenario, key)
    original = result.final_explanation_json
    changed, path, n = _with_number_bumped(original)
    assert changed != original, (path, n)  # N3
    assert n > 1, (path, n)  # 개수 1 이 아니라 계산된 수량을 바꿨다

    check = _check(result, changed)

    assert check.status is AgentOutputStatus.RETRY
    assert _codes(check) == {"VALUE_MISMATCH"}


@pytest.mark.parametrize("key", KEYS)
def test_changed_evidence_value_is_caught(scenario, key):
    """N2: 근거 참조 중 SKU 수량(/sku/…) 값 하나를 +1 하면 RETRY·VALUE_MISMATCH."""
    result = _passed(scenario, key)
    original = result.final_explanation_json
    changed = copy.deepcopy(original)
    refs = changed["evidence_references"]
    target = next(
        r for r in refs if r["path"].startswith("/sku/") and isinstance(r["value"], int)
    )
    target["value"] += 1
    assert changed != original  # N3

    check = _check(result, changed)

    assert check.status is AgentOutputStatus.RETRY
    assert _codes(check) == {"VALUE_MISMATCH"}


def test_number_finder_skips_codes_and_dates():
    """N3 장치: 코드 속 숫자·날짜는 바꾸지 않는다."""
    text = "CREAM20 100개, 2026-03-10 입고"
    assert re.sub(_INT.format(n=20), "21", text) == text
    assert re.sub(_INT.format(n=10), "11", text) == text
    assert re.sub(_INT.format(n=100), "101", text) == "CREAM20 101개, 2026-03-10 입고"
