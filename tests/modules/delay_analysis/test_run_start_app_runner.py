import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent.tools import (
    impact_tools,
    lookup_tools,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.agent.tracing import RunTracer
from app.modules.delay_analysis.application.run_start import app_runner as A
from app.modules.delay_analysis.application.run_start.engine_runtime import (
    ENGINE_TOOLS,
    missing_engine_tools,
)
from app.modules.delay_analysis.application.run_start.transitions import StartedRun

"""
    서버의 분석 실행기 설정과 실제 등록표 (GLOW-128) — DB 없음
      - 자동 시작은 기본 꺼짐. 켰는데 빠진 도구·빈 엔진 경로·자동 추적 환경 변수가 있으면 서버 시작을 멈춘다.
      - 실제 등록표는 develop 의 실제 도구 함수 12개를 그대로 등록한다(가짜가 섞이지 않음, T10 포함).
      - 실행 도구 묶음은 실행기가 넘긴 추적기를 그대로 쓴다.
      - 가상 시각이면 저장·설명 시각 = 실행 시작 시각, 아니면 None·서버 시각.
"""


def _settings(**changes):
    return get_settings().model_copy(update=changes)


def _run():
    return StartedRun(
        analysis_run_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        delay_event_id=uuid.uuid4(),
        operating_rule_set_id=uuid.uuid4(),
        input_seq=3,
        started_at=datetime(2026, 7, 2, 0, 0, 0, 123, tzinfo=UTC),
        trigger_shipment_schedule_snapshot_id=None,
        is_latest=True,
    )


def test_auto_start_is_off_by_default():
    assert (
        type(get_settings()).model_fields["analysis_auto_start_enabled"].default
        is False
    )


def test_all_contract_tools_are_registered():
    assert missing_engine_tools() == []
    assert len(ENGINE_TOOLS) == len(ToolName)


def test_engine_registry_uses_real_tool_functions():
    assert ENGINE_TOOLS[ToolName.GET_DELAY_CONTEXT].fn is lookup_tools.get_delay_context
    assert ENGINE_TOOLS[ToolName.GET_OPEN_ORDERS].fn is lookup_tools.get_open_orders
    assert (
        ENGINE_TOOLS[ToolName.CALCULATE_INVENTORY_IMPACT].fn
        is impact_tools.calculate_inventory_impact
    )
    assert (
        ENGINE_TOOLS[ToolName.IDENTIFY_AT_RISK_ORDERS].fn
        is impact_tools.identify_at_risk_orders
    )
    # 순수 함수는 pure_tool 로 감싸 이름만 같다
    for name in (
        ToolName.CHECK_OPTION_FEASIBILITY,
        ToolName.CALCULATE_OPTION_OUTCOMES,
        ToolName.COMPARE_OPTIONS,
        ToolName.VALIDATE_AGENT_OUTPUT,
    ):
        assert ENGINE_TOOLS[name].fn.__name__ == name.value
    t10 = ENGINE_TOOLS[ToolName.CALCULATE_OPTION_OUTCOMES]
    assert (t10.input_model.__name__, t10.output_model.__name__) == (
        "CalculateOptionOutcomesInput",
        "OptionOutcomes",
    )


def test_enabling_without_all_tools_stops_startup(monkeypatch, tmp_path):
    """도구가 빠진 경우를 흉내 낸다(지금 등록표는 다 있음)."""
    monkeypatch.setattr(
        A, "missing_engine_tools", lambda: [ToolName.CALCULATE_OPTION_OUTCOMES]
    )
    ready = {"glowship_engine_dir": tmp_path}

    with pytest.raises(A.AutoStartNotReadyError, match="calculate_option_outcomes"):
        A.check_auto_start_ready(
            _settings(analysis_auto_start_enabled=True, **ready), environ={}
        )
    A.check_auto_start_ready(
        _settings(analysis_auto_start_enabled=False, **ready), environ={}
    )


def test_enabling_with_everything_ready_passes(tmp_path):
    A.check_auto_start_ready(
        _settings(analysis_auto_start_enabled=True, glowship_engine_dir=tmp_path),
        environ={},
    )


def test_enabling_without_engine_dir_stops_startup(tmp_path):
    with pytest.raises(A.AutoStartNotReadyError, match="GLOWSHIP_ENGINE_DIR"):
        A.check_auto_start_ready(
            _settings(analysis_auto_start_enabled=True, glowship_engine_dir=None),
            environ={},
        )
    A.check_auto_start_ready(
        _settings(analysis_auto_start_enabled=False, glowship_engine_dir=None),
        environ={},
    )


def test_enabling_with_auto_tracing_env_stops_startup(tmp_path):
    """LangChain 자동 추적이 켜져 있으면 그래프 상태가 마스킹 없이 나갈 수 있어 켜지 않는다(agentflow 8.1)."""
    ready = _settings(analysis_auto_start_enabled=True, glowship_engine_dir=tmp_path)

    with pytest.raises(A.AutoStartNotReadyError, match="LANGCHAIN_TRACING_V2"):
        A.check_auto_start_ready(ready, environ={"LANGCHAIN_TRACING_V2": "true"})
    A.check_auto_start_ready(ready, environ={"LANGCHAIN_TRACING_V2": "false"})
    A.check_auto_start_ready(
        _settings(analysis_auto_start_enabled=False, glowship_engine_dir=tmp_path),
        environ={"LANGCHAIN_TRACING_V2": "true"},
    )


def test_engine_runtime_is_built_with_all_tools_and_tracer():
    run = _run()
    tracer = RunTracer(run.analysis_run_id, run.company_id, [])

    runtime = A.build_app_runner(_settings()).runtime_factory(object(), run, tracer)
    untraced = A.build_app_runner(_settings()).runtime_factory(object(), run, None)

    assert runtime.tracer is tracer
    assert untraced.tracer is None
    assert runtime.input_seq == run.input_seq
    assert runtime.registry.missing() == []
    tracer.close()


def test_runner_follows_settings():
    runner = A.build_app_runner(
        _settings(
            analysis_auto_start_enabled=False,
            analysis_max_concurrent_runs=2,
            analysis_shutdown_grace_seconds=3.0,
        )
    )

    assert runner.enabled is False
    assert runner.shutdown_grace_seconds == 3.0
    assert runner._semaphore._value == 2


def test_virtual_clock_passes_run_start_as_save_time():
    run = _run()
    virtual = A.build_app_runner(
        _settings(virtual_clock_start=datetime(2026, 7, 2, tzinfo=UTC))
    )
    real = A.build_app_runner(_settings(virtual_clock_start=None))

    assert virtual.save_time(run) == run.started_at
    assert real.save_time(run) is None
