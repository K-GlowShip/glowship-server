import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.modules.delay_analysis.application.agent.tools import (
    impact_tools,
    lookup_tools,
)
from app.modules.delay_analysis.application.agent.tools.common import ToolName
from app.modules.delay_analysis.application.run_start import app_runner as A
from app.modules.delay_analysis.application.run_start.engine_runtime import (
    ENGINE_TOOLS,
    missing_engine_tools,
)
from app.modules.delay_analysis.application.run_start.transitions import StartedRun

"""
    서버의 분석 실행기 설정과 실제 등록표 (GLOW-128) — DB 없음
      - 자동 시작은 기본 꺼짐. 켰는데 실제 등록표에 빠진 도구(지금은 T10)가 있으면 서버 시작을 멈춘다.
      - 실제 등록표는 develop 의 실제 도구 함수를 그대로 등록한다(가짜가 섞이지 않음).
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


def test_only_t10_is_missing_until_glow_133():
    assert missing_engine_tools() == [ToolName.CALCULATE_OPTION_OUTCOMES]


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
    for name in (
        ToolName.CHECK_OPTION_FEASIBILITY,
        ToolName.COMPARE_OPTIONS,
        ToolName.VALIDATE_AGENT_OUTPUT,
    ):
        assert ENGINE_TOOLS[name].fn.__name__ in {
            "check_option_feasibility",
            "compare_options",
            "validate_agent_output",
        }


def test_enabling_without_all_tools_stops_startup():
    with pytest.raises(A.AutoStartNotReadyError, match="calculate_option_outcomes"):
        A.check_auto_start_ready(_settings(analysis_auto_start_enabled=True))
    A.check_auto_start_ready(_settings(analysis_auto_start_enabled=False))


def test_engine_runtime_cannot_be_built_until_t10(monkeypatch):
    with pytest.raises(ValueError, match="calculate_option_outcomes"):
        A.build_app_runner(_settings()).runtime_factory(object(), _run())


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
