import logging
from typing import Any

from app.core.config import MOCK_EXPLANATION_MODEL, Settings, get_settings
from app.core.llm import (
    GENERATION_OPTION_KEYS,
    LlmConfigError,
    api_key_for,
    load_llm_library,
    split_model,
)
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)
from app.modules.delay_analysis.infrastructure.engine.explanation_assets import (
    REAL_MODEL_TEMPLATE_VERSION,
    TEMPLATE_ID,
    ExplanationAssetError,
    load_explanation_assets,
    load_real_model_config,
)
from app.modules.delay_analysis.infrastructure.explanation_models.adapter import (
    ExplanationModelAdapter,
)

"""
    AI 설명 모델 고르기 (GLOW-150, docs/agent-flow/explanation.md 8절)
      - 설정 EXPLANATION_MODEL("제공자/모델 ID")로 고른다. 기본 mock/mock-template-v1 은 엔진의 결정적 MockAdapter 다
        (외부 호출 없음, 모델 호출 라이브러리를 불러오지 않음).
      - new_explanation_adapter 는 부를 때마다 새 어댑터를 만든다. 설명 한 건마다 한 번 부른다.
      - 서버 시작 확인(check_explanation_model_ready): 실제 모델을 골랐을 때만 본다. 엔진 경로가 없거나, 제공자를 모르거나,
        키가 없거나, 모델 호출 라이브러리가 없거나, 엔진 등록부에 그 모델 설정이 없으면(또는 제공자가 다르면) 서버가 뜨지 않는다.
        등록부 확인은 엔진 generator.preflight 를 그대로 부른다(설명을 만들 때 엔진이 하는 것과 같은 검사).
      - 실제 모델은 프롬프트 1.1.2 와 엔진 등록부의 그 모델 설정을 쓴다(데이터 팀 승인 2026-11-17, gpt-6-luna).
        모델 설정의 settings(예: reasoning_effort "medium")는 호출마다 넘긴다. 서버가 모르는 키가 있으면 시작 단계에서 멈춘다
        (시험하지 않은 값을 제공자에 보내지 않음). mock 은 1.0.0 그대로다(explanation.md 8.13절).
"""

logger = logging.getLogger(__name__)


class ExplanationModelConfigError(RuntimeError):
    """실제 설명 모델을 쓸 수 없는 설정. 서버 시작 단계에서 올려 서버가 뜨지 않게 한다."""


def uses_mock_model(settings: Settings) -> bool:
    return settings.explanation_model == MOCK_EXPLANATION_MODEL


def configured_explanation_model(settings: Settings | None = None) -> str:
    """서버 설정의 설명 모델("제공자/모델 ID"). 실제로 쓰인 모델을 이것과 대조한다."""
    return (settings or get_settings()).explanation_model


def generation_settings(model_id: str, settings: Settings) -> dict[str, Any]:
    """
    엔진 등록부에 있는 그 모델 설정의 settings(호출에 넘길 생성 값). 엔진 경로가 없으면 빈 값이다
    (그때는 설명 생성 자체가 엔진을 불러오는 단계에서 멈춘다). 서버가 모르는 키가 있으면 ExplanationModelConfigError.
    """
    if settings.glowship_engine_dir is None:
        return {}
    values = dict(load_real_model_config(model_id).get("settings") or {})
    unknown = sorted(set(values) - GENERATION_OPTION_KEYS)
    if unknown:
        raise ExplanationModelConfigError(
            f"엔진 모델 설정 {model_id} 의 settings 에 서버가 넘기지 않는 값이 있습니다: {', '.join(unknown)}"
        )
    return values


def new_explanation_adapter(settings: Settings | None = None) -> Any:
    """설명 한 건에 쓸 새 어댑터. mock 이면 엔진 MockAdapter(라벨은 해시를 대조한 자산에서 읽는다)."""
    settings = settings or get_settings()
    if uses_mock_model(settings):
        mock = load_engine_module("explain.mock")
        return mock.MockAdapter(load_explanation_assets()["labels"])
    try:
        provider, model_id = split_model(settings.explanation_model)
        if provider == "mock":
            raise LlmConfigError(
                f"mock 모델은 {MOCK_EXPLANATION_MODEL} 하나입니다 (받은 값: {settings.explanation_model})"
            )
        api_key = api_key_for(settings, provider)
    except LlmConfigError as error:
        raise ExplanationModelConfigError(str(error)) from error
    return ExplanationModelAdapter(
        provider=provider,
        model_id=model_id,
        api_key=api_key,
        call_timeout_seconds=settings.explanation_model_timeout_seconds,
        total_budget_seconds=settings.explanation_total_budget_seconds,
        configured_model=settings.explanation_model,
        # 첫 호출 때 엔진 모델 설정에서 읽는다(어댑터를 만들 때는 엔진을 읽지 않음)
        generation_settings=lambda: generation_settings(model_id, settings),
    )


def check_explanation_model_ready(settings: Settings) -> bool:
    """서버 시작 단계 확인. mock 이면 아무것도 하지 않고 False, 실제 모델이 쓸 수 있는 상태면 True, 아니면 예외."""
    if uses_mock_model(settings):
        return False
    if settings.glowship_engine_dir is None:
        raise ExplanationModelConfigError(
            f"EXPLANATION_MODEL={settings.explanation_model} 인데 GLOWSHIP_ENGINE_DIR 이 없습니다"
            "(설명 생성은 엔진이 있어야 합니다)"
        )
    adapter = new_explanation_adapter(settings)
    try:
        load_llm_library()
    except LlmConfigError as error:
        raise ExplanationModelConfigError(str(error)) from error
    generator = load_engine_module("explain.generator")
    assets = load_engine_module("explain.assets")
    try:
        generator.preflight(adapter, TEMPLATE_ID, REAL_MODEL_TEMPLATE_VERSION)
    except assets.SystemConfigError as error:
        raise ExplanationAssetError(error.code, error.detail) from error
    options = generation_settings(adapter.model_id, settings)
    logger.info(
        "설명 모델 확인 완료: %s (프롬프트 %s@%s, 생성 값 %s)",
        settings.explanation_model,
        TEMPLATE_ID,
        REAL_MODEL_TEMPLATE_VERSION,
        options,
    )
    return True
