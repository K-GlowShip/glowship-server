import logging
from typing import Any

from app.core.config import MOCK_EXPLANATION_MODEL, Settings, get_settings
from app.core.llm import LlmConfigError, api_key_for, load_llm_library, split_model
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)
from app.modules.delay_analysis.infrastructure.engine.explanation_assets import (
    TEMPLATE_ID,
    TEMPLATE_VERSION,
    ExplanationAssetError,
    load_explanation_assets,
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
        등록된 모델 설정은 지금 mock 하나뿐이라, 데이터 팀이 새 엔진 판에 모델 설정을 넣기 전에는 실제 모델을 켤 수 없다.
"""

logger = logging.getLogger(__name__)


class ExplanationModelConfigError(RuntimeError):
    """실제 설명 모델을 쓸 수 없는 설정. 서버 시작 단계에서 올려 서버가 뜨지 않게 한다."""


def uses_mock_model(settings: Settings) -> bool:
    return settings.explanation_model == MOCK_EXPLANATION_MODEL


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
        generator.preflight(adapter, TEMPLATE_ID, TEMPLATE_VERSION)
    except assets.SystemConfigError as error:
        raise ExplanationAssetError(error.code, error.detail) from error
    logger.info("설명 모델 확인 완료: %s", settings.explanation_model)
    return True
