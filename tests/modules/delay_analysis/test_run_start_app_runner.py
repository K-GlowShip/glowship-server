import json
import uuid
from datetime import UTC, datetime

import pytest

from app.core.config import Settings, get_settings
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
from app.modules.delay_analysis.infrastructure.agent_trace_outbox import OUTBOX_DIR_NAME

"""
    서버의 분석 실행기 설정과 실제 등록표 (GLOW-128) — DB 없음
      - 자동 시작은 기본 꺼짐. 켰는데 빠진 도구·빈 엔진 경로·자동 추적 환경 변수가 있으면 서버 시작을 멈춘다.
      - 실제 등록표는 develop 의 실제 도구 함수 12개를 그대로 등록한다(가짜가 섞이지 않음, T10 포함).
      - 실행 도구 묶음은 실행기가 넘긴 추적기를 그대로 쓴다.
      - 가상 시각이면 저장·설명 시각 = 실행 시작 시각, 아니면 None·서버 시각.
      - 추적 내부 로그(이슈 #51): 폴더가 없으면 추적 안 함, 있으면 고객사·UTC 날짜 파일에 마스킹해 남김, 싱크 하나 공유,
        켰는데 폴더가 비면 경고·쓸 수 없으면 시작 멈춤, 빈 값은 설정 안 한 것.
"""


def _settings(**changes):
    # 개발자 .env 의 AGENT_TRACE_DIR 에 좌우되지 않게 추적 폴더는 기본 없음(넘기면 그 값)
    return get_settings().model_copy(update={"agent_trace_dir": None, **changes})


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


# --- 추적 내부 로그 (이슈 #51, docs/agent-flow/agent-tracing.md 3절) ---------------------------


def test_no_trace_dir_means_no_tracing():
    assert A.build_app_runner(_settings(agent_trace_dir=None)).tracer_factory is None


def test_trace_dir_writes_masked_events_to_internal_log(tmp_path):
    """폴더가 있으면 실행마다 추적기를 만들고, 사건은 고객사·UTC 날짜 파일에 마스킹돼 한 줄씩 남는다."""
    run = _run()
    tracer = A.build_app_runner(_settings(agent_trace_dir=tmp_path)).tracer_factory(run)

    tracer.emit(
        "RUN_START", "analysis", "STARTED", summary={"note": "A-ORD-20260701-0001 확인"}
    )
    assert tracer.close()
    files = list((tmp_path / OUTBOX_DIR_NAME / str(run.company_id)).glob("*.jsonl"))
    lines = files[0].read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[0])

    assert (len(files), len(lines)) == (1, 1)
    assert (event["analysis_run_id"], event["kind"]) == (
        str(run.analysis_run_id),
        "RUN_START",
    )
    assert files[0].stem == event["at"][:10]  # 서버 시각(UTC) 날짜, 가상 시각 아님
    assert "A-ORD-" not in lines[0]


def test_runs_share_one_trace_sink(tmp_path):
    """줄이 섞이지 않게 하는 잠금이 싱크마다라서, 동시에 도는 실행이 싱크 하나를 같이 쓴다."""
    factory = A.build_app_runner(_settings(agent_trace_dir=tmp_path)).tracer_factory
    first, second = factory(_run()), factory(_run())

    assert first._sinks[0] is second._sinks[0]
    first.close()
    second.close()


def test_enabling_with_unwritable_trace_dir_stops_startup(tmp_path):
    """넣었는데 쓸 수 없으면 서버 시작을 멈춘다(실행마다 쓰기 경고가 쌓이는 것보다 바로 알림)."""
    blocked = tmp_path / "not-a-folder"
    blocked.write_text("x")
    ready = {"glowship_engine_dir": tmp_path}
    writable = tmp_path / "trace"

    with pytest.raises(A.AutoStartNotReadyError, match="AGENT_TRACE_DIR"):
        A.check_auto_start_ready(
            _settings(
                analysis_auto_start_enabled=True, agent_trace_dir=blocked, **ready
            ),
            environ={},
        )
    A.check_auto_start_ready(
        _settings(analysis_auto_start_enabled=False, agent_trace_dir=blocked, **ready),
        environ={},
    )
    A.check_auto_start_ready(
        _settings(analysis_auto_start_enabled=True, agent_trace_dir=writable, **ready),
        environ={},
    )
    assert list((writable / OUTBOX_DIR_NAME).iterdir()) == []  # 확인 파일은 남지 않음


def test_enabling_without_trace_dir_only_warns(tmp_path, caplog):
    A.check_auto_start_ready(
        _settings(
            analysis_auto_start_enabled=True,
            glowship_engine_dir=tmp_path,
            agent_trace_dir=None,
        ),
        environ={},
    )

    assert "AGENT_TRACE_DIR" in caplog.text


def _settings_from_env(**env):
    return Settings(
        _env_file=None,
        postgres_host="h",
        postgres_port=5432,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        jwt_secret_key="x" * 32,
        **env,
    )


def test_relative_and_home_trace_dir_become_absolute(monkeypatch, tmp_path):
    """엔진 경로처럼 절대 경로로 바꾼다. 그대로 두면 "~/trace" 가 실행 폴더 아래 "~" 폴더가 된다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    relative = _settings_from_env(agent_trace_dir="rel/trace").agent_trace_dir
    home = _settings_from_env(agent_trace_dir="~/trace").agent_trace_dir

    assert relative == (tmp_path / "rel" / "trace").resolve()
    assert home == (tmp_path / "home" / "trace").resolve()
    assert relative.is_absolute() and home.is_absolute()


@pytest.mark.parametrize("raw", ["", "   "])
def test_blank_trace_dir_setting_means_off(monkeypatch, raw):
    # `AGENT_TRACE_DIR=` 로 비워 두면 Path("") 가 "." 이 되어 현재 폴더에 쓰게 된다
    monkeypatch.setenv("AGENT_TRACE_DIR", raw)
    settings = Settings(
        _env_file=None,
        postgres_host="h",
        postgres_port=5432,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        jwt_secret_key="x" * 32,
    )

    assert settings.agent_trace_dir is None
