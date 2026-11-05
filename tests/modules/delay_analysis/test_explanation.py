import asyncio
import copy
import json
import random
import shutil
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent import explanation as X
from app.modules.delay_analysis.application.agent.retry import (
    EXPLANATION_GENERATION_FAILED,
)
from app.modules.delay_analysis.application.agent.retry import (
    MAX_ATTEMPTS as RETRY_MAX_ATTEMPTS,
)
from app.modules.delay_analysis.application.agent.state import (
    AnalysisOutcome,
    AnalysisStep,
)
from app.modules.delay_analysis.application.agent.tools.output_validation import (
    validate_agent_output,
)
from app.modules.delay_analysis.application.agent.tools.validation_schemas import (
    MAX_EXPLANATION_ATTEMPTS,
    AgentOutputStatus,
    ValidateAgentOutputInput,
)
from app.modules.delay_analysis.infrastructure.engine import explanation_assets as XA
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

"""
    AI 설명 생성 (GLOW-127, docs/agent-flow/explanation.md 5절)
      1. 서버 감싸기 결과 = 엔진 generator 를 직접 부른 결과 (GS-05 골든, 결함 18종, 무작위 READY 실행)
      2. 3번 실패 → 실행 실패 요약(#28 explanation_failure) 모양, 실패 원문·canary·이메일은 결과에 없음
      3. 자산 오류는 시도가 아님(모델 호출 0번), READY 가 아니면 모델을 부르지 않음, 시각은 시간대 필수·UTC 초
      4. 추적기는 시도 기록을 받고, 추적기가 망가져도 결과는 같음
      5. 시작 단계 자산 확인: 해시가 다르거나 파일이 없으면 멈추고, 엔진 경로가 없으면 건너뜀
    엔진 자체 시험(test_explain 97건)은 test_engine_loader.py 가 돌린다. 여기서는 서버 경계만 본다.
"""

RUN_ID = "11111111-2222-3333-4444-555555555555"
NOW = datetime(2026, 3, 10, 0, 5, tzinfo=UTC)
NOW_TEXT = "2026-03-10T00:05:00Z"
SHIP = "SHIP-DELAYED"

# 엔진 explain/mock.py FAULTS (실행 전 고정. 엔진이 결함을 더하면 test_fault_list_matches_engine 가 실패)
FAULTS = [
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
    "timeout",
    "call_error",
]
RT2_FAULTS = {
    1: "wrong_number",
    2: "forbidden_phrase",
    3: "timeout",
}  # 엔진 run_s1_s5.RT2_FAULTS


@pytest.fixture(scope="module")
def engine():
    if get_settings().glowship_engine_dir is None:
        pytest.skip("GLOWSHIP_ENGINE_DIR 이 없어 설명 생성 시험을 건너뜀")
    return {
        "pipeline": load_engine_module("pipeline"),
        "generator": load_engine_module("explain.generator"),
        "input_doc": load_engine_module("explain.input_doc"),
        "mock": load_engine_module("explain.mock"),
        "gs05": load_engine_module("tests.fixture_gs05"),
        "u6": load_engine_module("tests.fixture_u6_donor"),
        "test_explain": load_engine_module("tests.test_explain"),
        "labels": XA.load_explanation_assets()["labels"],
    }


@pytest.fixture
def gs05(engine, monkeypatch):
    """
    canary 를 고정한다. 엔진 생성기는 시도마다 secrets.token_hex 로 canary 를 만드는데, 16진 숫자가 우연히
    전화번호 금지 패턴(LK-05)에 맞으면 leak_canary 결함의 사유 코드가 하나 늘어 엔진 직접 호출과 달라진다(11-06 확인).
    고정값 "ab…" 은 어떤 LK 패턴에도 맞지 않는다.
    """
    monkeypatch.setattr(engine["generator"].secrets, "token_hex", lambda n: "ab" * n)
    snap, _ = engine["gs05"].build_snap()
    return snap, engine["pipeline"].analyze(snap)


def test_canary_is_fixed_in_these_tests(engine, gs05):
    """위 고정이 실제로 걸리는지(고정이 빠지면 같은 결함 비교가 운에 따라 실패한다)"""
    assert engine["generator"].secrets.token_hex(12) == "ab" * 12


class FakeTracer:
    def __init__(self, broken: bool = False):
        self.broken = broken
        self.calls: list[list[dict]] = []

    def explanation_attempts(self, attempts, usage=()):
        self.calls.append([dict(a) for a in attempts])
        if self.broken:
            raise RuntimeError("추적기 장애")


def _run(
    snap, analysis, *, adapter=None, tracer=None, now=NOW, run_id=RUN_ID, ship=SHIP
):
    return asyncio.run(
        X.generate_explanation(
            snap=snap,
            analysis=analysis,
            analysis_run_id=run_id,
            shipment_code=ship,
            now=now,
            adapter=adapter,
            tracer=tracer,
        )
    )


def _direct(engine, snap, analysis, faults=None, run_id=RUN_ID, ship=SHIP):
    """엔진 하네스(run_s1_s5.explain_run)와 같은 방식으로 엔진을 직접 부른다."""
    doc = engine["input_doc"].build(snap, analysis, run_id, ship)
    adapter = engine["mock"].MockAdapter(engine["labels"], faults or {})
    return engine["generator"].generate(doc, adapter, NOW_TEXT)


# --- 1. 엔진과 같은 결과 -----------------------------------------------------------------------


def test_fault_list_matches_engine(engine):
    assert FAULTS == list(engine["mock"].FAULTS)
    assert set(FAULTS) == set(engine["test_explain"].FAULT_EXPECT)


def test_attempt_limit_matches_engine_and_retry_rule(engine):
    assert (
        MAX_EXPLANATION_ATTEMPTS
        == engine["generator"].MAX_ATTEMPTS
        == RETRY_MAX_ATTEMPTS
        == 3
    )


def test_passed_equals_engine_generate(engine, gs05):
    snap, analysis = gs05
    result = _run(snap, analysis)
    direct = _direct(engine, snap, analysis)

    assert isinstance(result, X.ExplanationPassed)
    assert direct["status"] == "PASSED"
    assert result.final_explanation_json == direct["explanation"]
    assert result.model_name == direct["model_name"] == "mock/mock-template-v1"
    assert result.prompt_version == direct["prompt_version"] == "explain_ready_v1@1.0.0"
    assert list(result.attempts) == direct["attempts"]
    assert result.generated_at == NOW
    assert result.input_document == engine["input_doc"].build(
        snap, analysis, RUN_ID, SHIP
    )


def test_passed_matches_engine_golden_text(engine, gs05):
    """엔진 test_explain 의 GS-05 골든(실행 전 작성한 문장)과 글자까지 같다"""
    golden = engine["test_explain"].GOLDEN_GS05
    out = _run(*gs05).final_explanation_json

    assert out["run_summary"] == golden["run_summary"]
    assert out["risk_summary"] == golden["risk_summary"]
    assert {o["option_type"]: o["text"] for o in out["option_explanations"]} == golden[
        "options"
    ]
    assert out["recommendation_explanation"] == golden["recommendation_explanation"]
    assert out["warnings"] == golden["warnings"]
    evidence = {r["path"]: r["value"] for r in out["evidence_references"]}
    assert {p: evidence[p] for p in golden["evidence"]} == golden["evidence"]
    # 메타데이터는 모델이 아니라 서버가 준 값
    assert out["analysis_run_id"] == RUN_ID
    assert out["generated_at"] == NOW_TEXT
    assert out["decision_signature_hash"] == gs05[1]["sha"]


@pytest.mark.parametrize("fault", FAULTS)
def test_three_same_faults_fail_like_engine(engine, gs05, fault):
    snap, analysis = gs05
    faults = {1: fault, 2: fault, 3: fault}
    labels = engine["labels"]
    result = _run(snap, analysis, adapter=engine["mock"].MockAdapter(labels, faults))
    direct = _direct(engine, snap, analysis, faults)

    assert isinstance(result, X.ExplanationFailed)
    assert direct["status"] == "FAILED"
    assert result.failure_message == direct["failure_message"]
    assert list(result.attempts) == direct["attempts"]
    expected_code = engine["test_explain"].FAULT_EXPECT[fault]
    for attempt in result.attempts:
        assert expected_code in {code for code, _ in attempt["codes"]}
    assert [a["retry_no"] for a in result.attempts] == [1, 2, 3]


@pytest.mark.parametrize("fault", ["wrong_number", "timeout", "leak_canary"])
def test_fault_on_first_attempt_then_passes(engine, gs05, fault):
    adapter = engine["mock"].MockAdapter(engine["labels"], {1: fault})
    result = _run(*gs05, adapter=adapter)

    assert isinstance(result, X.ExplanationPassed)
    assert adapter.calls == 2
    assert [a["retry_no"] for a in result.attempts] == [1, 2]
    assert result.attempts[0]["codes"] and not result.attempts[1]["codes"]
    assert result.final_explanation_json["retry_no"] == 2
    assert result.expected_metadata().retry_no == 2


def test_random_ready_runs_equal_engine(engine):
    """엔진 test_explain.ready_docs 와 같은 방식으로 바꾼 READY 실행 20건: 감싸기 = 엔진 직접 호출"""
    rnd = random.Random(20261105)
    checked = 0
    for i in range(40):
        snap = engine["u6"].world(rnd.choice([0, 60, 200, 300]))
        dest = snap["dest"]["warehouse_id"]
        snap["delayed_qty"]["X"] = rnd.randint(50, 600)
        snap["mu"][(dest, "X")] = rnd.choice([3.0, 10.0, 18 / 7, 27.5])
        snap["mu"][(dest, "Y")] = rnd.choice([0.5, 2.0, 5.0])
        snap["kr_on_hand"]["X"] = rnd.choice([0, 50, 300])
        analysis = engine["pipeline"].analyze(snap)
        if analysis["status"] != "READY_FOR_REVIEW":
            continue
        ship = f"SHIP-{i:03d}"
        result = _run(snap, analysis, ship=ship)
        direct = _direct(engine, snap, analysis, ship=ship)
        assert isinstance(result, X.ExplanationPassed), i
        assert result.final_explanation_json == direct["explanation"], i
        checked += 1
    assert checked >= 20


# --- 2. 3번 실패 → 실행 실패 요약 ----------------------------------------------------------------


def test_rt2_failure_gives_run_failure_summary(engine, gs05):
    """엔진 RT-2(숫자 틀림 → 금지 표현 → 시간 초과) = overlay 설명 실패 실행을 만든 주입"""
    adapter = engine["mock"].MockAdapter(engine["labels"], RT2_FAULTS)
    result = _run(*gs05, adapter=adapter)

    assert isinstance(result, X.ExplanationFailed)
    assert adapter.calls == 3
    assert result.failure_message == (
        "1:VALUE_MISMATCH@risk_summary#s1;"
        "2:FORBIDDEN_EXPRESSION@recommendation_explanation:PF-01;"
        "3:MODEL_TIMEOUT@adapter"
    )
    summary = result.failure_summary()
    assert summary.status is AnalysisOutcome.FAILED
    assert summary.failed_step is AnalysisStep.VALIDATING_OUTPUT
    assert summary.failure_code == EXPLANATION_GENERATION_FAILED
    assert summary.attempt_count == 3
    assert summary.retry_exhausted is True
    assert summary.failure_message.endswith(f"(시도: {result.failure_message})")


@pytest.mark.parametrize("fault", ["leak_email", "leak_canary", "bad_json"])
def test_failed_result_keeps_no_raw_output(engine, gs05, fault):
    faults = {1: fault, 2: fault, 3: fault}
    seen_canaries = []
    adapter = engine["mock"].MockAdapter(engine["labels"], faults)
    original = adapter.complete

    def spy(prompt, request):
        seen_canaries.append(request["canary"])
        return original(prompt, request)

    adapter.complete = spy
    result = _run(*gs05, adapter=adapter)
    text = repr(result) + repr(result.failure_summary())

    assert isinstance(result, X.ExplanationFailed)
    assert "ops@example.com" not in text
    assert "JSON이 아닙니다" not in text
    assert len(seen_canaries) == 3
    assert all(c not in text for c in seen_canaries)
    assert not hasattr(result, "final_explanation_json")


# --- 3. 시도로 세지 않는 경우 --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"model_id": "mock-unknown"}, "ASSET_NOT_REGISTERED"),
        ({"provider": "other"}, "MODEL_CONFIG_MISMATCH"),
    ],
)
def test_asset_error_is_not_an_attempt(engine, gs05, kwargs, code):
    adapter = engine["mock"].MockAdapter(engine["labels"], **kwargs)
    tracer = FakeTracer()
    with pytest.raises(XA.ExplanationAssetError) as caught:
        _run(*gs05, adapter=adapter, tracer=tracer)
    assert caught.value.code == code
    assert adapter.calls == 0
    assert tracer.calls == []


@pytest.mark.parametrize("status", ["NO_RISK", "FAILED", "ON_HOLD"])
def test_only_ready_runs_get_explanation(engine, gs05, status):
    """서버가 먼저 막는다(엔진 input_doc.build 도 막지만, 엔진 규칙이 바뀌어도 서버 확인은 남게). 문구로 구분한다"""
    snap, analysis = gs05
    other = dict(analysis, status=status)
    adapter = engine["mock"].MockAdapter(engine["labels"])
    with pytest.raises(
        ValueError, match=f"READY_FOR_REVIEW 실행만 만듭니다\\(현재 {status}\\)"
    ):
        _run(snap, other, adapter=adapter)
    assert adapter.calls == 0


def test_naive_time_is_rejected_before_model_call(engine, gs05):
    adapter = engine["mock"].MockAdapter(engine["labels"])
    with pytest.raises(ValueError, match="시간대"):
        _run(*gs05, adapter=adapter, now=datetime(2026, 3, 10, 0, 5))
    assert adapter.calls == 0


def test_time_is_converted_to_utc_seconds(gs05):
    kst = timezone(timedelta(hours=9))
    result = _run(*gs05, now=datetime(2026, 3, 10, 9, 5, 0, 987654, tzinfo=kst))

    assert result.generated_at == NOW
    assert result.final_explanation_json["generated_at"] == NOW_TEXT
    assert result.expected_metadata().generated_at == NOW_TEXT


def test_engine_inputs_are_not_changed(gs05):
    snap, analysis = gs05
    before = copy.deepcopy((snap, analysis))
    _run(snap, analysis)
    assert (snap, analysis) == before


# --- 4. 추적 -------------------------------------------------------------------------------------


def test_tracer_receives_engine_attempts(engine, gs05):
    tracer = FakeTracer()
    result = _run(
        *gs05,
        adapter=engine["mock"].MockAdapter(engine["labels"], RT2_FAULTS),
        tracer=tracer,
    )
    assert tracer.calls == [list(result.attempts)]
    assert [a["retry_no"] for a in tracer.calls[0]] == [1, 2, 3]


def test_broken_tracer_does_not_change_result(engine, gs05):
    healthy = _run(*gs05)
    broken = _run(*gs05, tracer=FakeTracer(broken=True))
    assert broken == healthy


# --- 5. T12 로 다시 검증 -------------------------------------------------------------------------


def test_passed_explanation_rechecks_as_passed(gs05):
    result = _run(*gs05)
    check = validate_agent_output(
        ValidateAgentOutputInput(
            input_document=result.input_document,
            output=json.dumps(result.final_explanation_json, ensure_ascii=False),
            expected_metadata=result.expected_metadata(),
            canary=None,
        )
    )
    assert check.status is AgentOutputStatus.PASSED
    assert check.errors == []


def test_recheck_catches_changed_number(gs05):
    """음성: 통과한 설명의 숫자 하나를 바꾸면 T12 가 잡는다(시도 1이므로 설명 재요청)"""
    result = _run(*gs05)
    changed = copy.deepcopy(result.final_explanation_json)
    changed["risk_summary"] = changed["risk_summary"].replace(
        "2개입니다", "3개입니다", 1
    )
    assert changed["risk_summary"] != result.final_explanation_json["risk_summary"]
    check = validate_agent_output(
        ValidateAgentOutputInput(
            input_document=result.input_document,
            output=json.dumps(changed, ensure_ascii=False),
            expected_metadata=result.expected_metadata(),
            canary=None,
        )
    )
    assert check.status is AgentOutputStatus.RETRY
    assert "VALUE_MISMATCH" in {e.code for e in check.errors}


# --- 6. 시작 단계 자산 확인 ----------------------------------------------------------------------


def _asset_copy(tmp_path):
    source = load_engine_module("explain.assets").ASSETS
    target = tmp_path / "assets"
    shutil.copytree(source, target)
    return target


def test_startup_asset_check_passes_on_real_assets(engine):
    assert XA.verify_explanation_assets() is True


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("labels/ko-v1.json", "LABELS_SHA256_MISMATCH"),
        ("prompts/explain_ready_v1/1.0.0.md", "PROMPT_SHA256_MISMATCH"),
        ("forbidden_expressions/ko-v2.json", "FORBIDDEN_SHA256_MISMATCH"),
        ("schemas/explanation.v1.schema.json", "SCHEMA_SHA256_MISMATCH"),
        ("model_configs/mock-template-v1.json", "MODEL_CONFIG_MISMATCH"),
    ],
)
def test_startup_asset_check_stops_on_changed_file(engine, tmp_path, change, code):
    base = _asset_copy(tmp_path)
    path = base / change
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(XA.ExplanationAssetError) as caught:
        XA.verify_explanation_assets(base=base)
    assert caught.value.code == code


def test_startup_asset_check_stops_on_missing_file(engine, tmp_path):
    base = _asset_copy(tmp_path)
    (base / "prompts" / "explain_ready_v1" / "1.0.0.md").unlink()
    with pytest.raises(XA.ExplanationAssetError) as caught:
        XA.verify_explanation_assets(base=base)
    assert caught.value.code == "PROMPT_FILE_MISSING"


def test_startup_asset_check_stops_on_unregistered_model_config(engine):
    with pytest.raises(XA.ExplanationAssetError) as caught:
        XA.verify_explanation_assets("gpt-unapproved")
    assert caught.value.code == "ASSET_NOT_REGISTERED"


def test_startup_asset_check_skips_without_engine_dir(monkeypatch):
    class NoEngine:
        glowship_engine_dir = None

    monkeypatch.setattr(XA, "get_settings", lambda: NoEngine())
    assert XA.verify_explanation_assets() is False
