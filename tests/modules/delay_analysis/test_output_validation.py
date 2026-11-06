import json
import re

import pytest
from pydantic import ValidationError

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent.tools import (
    validation_schemas as V,
)
from app.modules.delay_analysis.application.agent.tools.output_validation import (
    validate_agent_output,
)
from app.modules.delay_analysis.infrastructure.engine import explanation_assets as XA
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)
from tests.modules.delay_analysis.test_tool_schemas import (
    _doc,
    _doc_paths,
    _model_paths,
    _section,
    _tables,
)

"""
    T12 validate_agent_output (docs/agent-flow/tool-contracts.md 8절, GLOW-127)
      1. 계약 8절 표 = 모델 필드, 판정 규칙(PASSED·RETRY·FAILED)이 문서와 같음
      2. T12 판정 = 엔진 explain.validator 를 직접 부른 결과 (결함 16종, 시도 번호별 판정)
      3. 입력 모델이 연결 오류(다른 실행·판정 서명·문서 판)와 값 모양을 거부함
      4. 사유 코드 목록 = 엔진 검증기·생성기가 내는 코드
"""

RUN_ID = "11111111-2222-3333-4444-555555555555"
NOW_TEXT = "2026-03-10T00:05:00Z"
CANARY = "CNRY-T12"
# 모델 출력이 있는 결함만(시간 초과·호출 오류는 출력이 없어 생성기가 코드를 붙임)
OUTPUT_FAULTS = [
    "wrong_number",
    "wrong_date",
    "missing_key",
    "forbidden_phrase",
    "hash_mismatch",
    "unreferenced_number",
    "leak_email",
    "leak_canary",
    "too_long",
    "wrong_recommendation",
    "drop_option",
    "wrong_status",
    "bad_json",
    "wrong_language",
    "wrong_evidence_value",
    "wrong_code",
]


@pytest.fixture(scope="module")
def engine():
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 T12 시험을 건너뜀")
    snap, _ = load_engine_module("tests.fixture_gs05").build_snap()
    analysis = load_engine_module("pipeline").analyze(snap)
    doc = load_engine_module("explain.input_doc").build(
        snap, analysis, RUN_ID, "SHIP-DELAYED"
    )
    assets = XA.load_explanation_assets()
    return {
        "doc": doc,
        "assets": assets,
        "mock": load_engine_module("explain.mock"),
        "validator": load_engine_module("explain.validator"),
        "test_explain": load_engine_module("tests.test_explain"),
    }


def _meta(engine, retry_no=1) -> dict:
    run = engine["doc"]["run"]
    return {
        "schema_version": "explanation.v1",
        "language": "ko",
        "analysis_run_id": run["analysis_run_id"],
        "decision_signature_hash": run["decision_signature_sha256"],
        "model_provider": "mock",
        "model_id": "mock-template-v1",
        "prompt_template_id": "explain_ready_v1",
        "prompt_template_version": "1.0.0",
        "prompt_template_sha256": engine["assets"]["prompt"]["sha256"],
        "generated_at": NOW_TEXT,
        "retry_no": retry_no,
    }


def _output(engine, meta, fault=None) -> str:
    mock = engine["mock"]
    out = mock.render(engine["doc"], engine["assets"]["labels"], meta)
    if fault:
        return mock.inject(out, fault, engine["assets"]["labels"], CANARY)
    return json.dumps(out, ensure_ascii=False)


def _input(engine, output, meta, canary=CANARY) -> V.ValidateAgentOutputInput:
    return V.ValidateAgentOutputInput(
        input_document=engine["doc"],
        output=output,
        expected_metadata=meta,
        canary=canary,
    )


def _engine_errors(engine, output, meta, canary=CANARY):
    a = engine["assets"]
    ctx = {
        "input_doc": engine["doc"],
        "labels": a["labels"],
        "forbidden": a["forbidden"],
        "schema": a["schema"],
        "expected": meta,
        "canary": canary,
    }
    return [(c, loc) for c, loc in engine["validator"].validate(output, ctx)]


# --- 1. 계약 8절 = 모델 --------------------------------------------------------------------------


def test_section8_tables_match_models():
    tables = _tables(_section("## 8.", _doc()))
    assert _doc_paths(tables[0]) == _model_paths(V.ValidateAgentOutputInput)
    assert _doc_paths(tables[1]) == _model_paths(V.AgentOutputValidation)


def test_section8_status_rules_match_enum():
    rows = _tables(_section("## 8.", _doc()))[2]
    documented = [re.findall(r"`([A-Z_]+)`", row[0])[0] for row in rows]
    assert documented == [s.value for s in V.AgentOutputStatus]


def test_section8_error_codes_match_enum():
    rows = _tables(_section("## 8.", _doc()))[3]
    documented = {re.findall(r"`([A-Z_]+)`", row[0])[0] for row in rows}
    assert documented == {c.value for c in V.ExplanationErrorCode}


def test_error_codes_match_engine_source():
    """엔진 검증기가 add("코드", …) 로 내는 코드 + 생성기의 MODEL_* = 서버 열거형"""
    engine_dir = get_settings().glowship_engine_dir
    if engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없음")
    explain = engine_dir / "glowship_engine" / "explain"
    validator = (explain / "validator.py").read_text(encoding="utf-8")
    generator = (explain / "generator.py").read_text(encoding="utf-8")
    codes = set(re.findall(r'add\(\s*"([A-Z_]+)"', validator))
    # 생성기의 시도 기록 코드 (MODEL_CONFIG_MISMATCH 는 SystemConfigError 코드라 시도 기록에 안 나옴)
    codes |= set(re.findall(r'\("(MODEL_[A-Z_]+)", "adapter"\)', generator))
    codes |= set(re.findall(r'"(LEAKAGE_DETECTED|FORBIDDEN_EXPRESSION)"', validator))
    assert codes == {c.value for c in V.ExplanationErrorCode}


# --- 2. 엔진 검증기와 같은 판정 ------------------------------------------------------------------


def test_clean_output_passes(engine):
    meta = _meta(engine)
    result = validate_agent_output(_input(engine, _output(engine, meta), meta))
    assert result.status is V.AgentOutputStatus.PASSED
    assert result.errors == []


@pytest.mark.parametrize("fault", OUTPUT_FAULTS)
@pytest.mark.parametrize(
    ("retry_no", "status"),
    [
        (1, V.AgentOutputStatus.RETRY),
        (2, V.AgentOutputStatus.RETRY),
        (3, V.AgentOutputStatus.FAILED),
    ],
)
def test_faulty_output_matches_engine_validator(engine, fault, retry_no, status):
    meta = _meta(engine, retry_no)
    output = _output(engine, meta, fault)
    result = validate_agent_output(_input(engine, output, meta))
    expected = _engine_errors(engine, output, meta)

    assert expected, "엔진이 결함을 잡아야 시험이 뜻이 있다"
    assert [(e.code.value, e.location) for e in result.errors] == expected
    assert result.status is status
    assert engine["test_explain"].FAULT_EXPECT[fault] in {e.code for e in result.errors}


def test_canary_leak_needs_canary(engine):
    """canary 를 모르면(null) 그 유출 검사만 빠진다. 생성 중 시도별 검증은 엔진이 canary 를 넣어 한다"""
    meta = _meta(engine)
    output = _output(engine, meta, "leak_canary")
    with_canary = validate_agent_output(_input(engine, output, meta, canary=CANARY))
    without = validate_agent_output(_input(engine, output, meta, canary=None))

    assert ("LEAKAGE_DETECTED", "canary") in {
        (e.code, e.location) for e in with_canary.errors
    }
    assert ("LEAKAGE_DETECTED", "canary") not in {
        (e.code, e.location) for e in without.errors
    }


def test_metadata_from_output_is_not_trusted(engine):
    """출력의 메타데이터가 아니라 기대값과 맞춰 본다: 생성 시각·시도 번호가 다르면 SCHEMA_INVALID"""
    meta = _meta(engine, retry_no=2)
    output = _output(engine, _meta(engine, retry_no=1))
    result = validate_agent_output(_input(engine, output, meta))
    assert ("SCHEMA_INVALID", "meta:retry_no") in {
        (e.code, e.location) for e in result.errors
    }


# --- 3. 입력 모델 --------------------------------------------------------------------------------


def _valid_kwargs(engine) -> dict:
    meta = _meta(engine)
    return {
        "input_document": engine["doc"],
        "output": "{}",
        "expected_metadata": meta,
        "canary": None,
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda k: k["expected_metadata"].update(
                analysis_run_id="aaaaaaaa-2222-3333-4444-555555555555"
            ),
            "analysis_run_id",
        ),
        (
            lambda k: k["expected_metadata"].update(decision_signature_hash="0" * 64),
            "decision_signature_hash",
        ),
        (
            lambda k: k.update(
                input_document=dict(
                    k["input_document"], schema_version="explanation_input.v0"
                )
            ),
            "explanation_input.v1",
        ),
        (lambda k: k["expected_metadata"].update(retry_no=0), "retry_no"),
        (lambda k: k["expected_metadata"].update(retry_no=4), "retry_no"),
        (lambda k: k["expected_metadata"].update(retry_no="1"), "retry_no"),
        (
            lambda k: k["expected_metadata"].update(
                generated_at="2026-03-10T09:05:00+09:00"
            ),
            "generated_at",
        ),
        (lambda k: k["expected_metadata"].update(language="en"), "language"),
        (lambda k: k.update(output=None), "output"),
        (lambda k: k.update(extra=1), "extra"),
    ],
)
def test_input_model_rejects(engine, change, message):
    kwargs = _valid_kwargs(engine)
    change(kwargs)
    with pytest.raises(ValidationError, match=message):
        V.ValidateAgentOutputInput(**kwargs)


def test_input_model_accepts_valid(engine):
    V.ValidateAgentOutputInput(**_valid_kwargs(engine))


@pytest.mark.parametrize(
    ("status", "errors"),
    [
        ("PASSED", [{"code": "VALUE_MISMATCH", "location": "x"}]),
        ("RETRY", []),
        ("FAILED", []),
    ],
)
def test_output_model_status_must_match_errors(status, errors):
    with pytest.raises(ValidationError, match="PASSED"):
        V.AgentOutputValidation(status=status, errors=errors)


def test_output_model_rejects_unknown_code():
    with pytest.raises(ValidationError):
        V.AgentOutputValidation(
            status="RETRY", errors=[{"code": "NEW_CODE", "location": "x"}]
        )
