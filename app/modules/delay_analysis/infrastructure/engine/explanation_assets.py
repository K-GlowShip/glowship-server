import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_engine_module,
)

"""
    AI 설명 자산 확인 (docs/agent-flow/explanation.md 3절)
      - 자산 = 프롬프트·설명 스키마(explanation.v1)·금지 표현·라벨·모델 설정. 엔진 explain/assets 폴더에 있고,
        엔진 explain.assets.load 가 등록부(registry_v2.json)의 SHA-256 과 파일을 하나씩 대조한다.
      - 자산이 없거나 해시가 다르면 엔진은 SystemConfigError 를 낸다. 이것은 모델 호출 전 시스템 구성 오류라
        설명 시도로 세지 않고 실행 상태도 바꾸지 않는다(엔진 generator 머리말). 서버는 ExplanationAssetError 로 바꿔 올린다.
      - 여기서 확인하는 모델 설정은 mock(mock-template-v1)이다. 실제 모델을 고르면 그 모델의 설정은
        explanation_models.selection.check_explanation_model_ready 가 시작 단계에서 확인한다(GLOW-150).
"""

logger = logging.getLogger(__name__)

TEMPLATE_ID = "explain_ready_v1"
# mock 이 쓰는 프롬프트 판. mock 은 프롬프트를 읽지 않지만 판 표기는 설명에 저장되고, 확정 DB 정답 설명이 이 판으로 만들어져 있다.
TEMPLATE_VERSION = "1.0.0"
# 실제 모델이 쓰는 프롬프트 판(데이터 팀 승인 2026-11-17, 계획 저장소 tools/glowship_engine "엔진 수정 버전").
# 1.0.0 은 출력 형식·라벨·필수 근거를 주지 않아 실제 모델 설명이 검증을 통과하지 못했다(explanation.md 8.13절).
# 1.1.2 는 1.1.1 에 추천 이유 문장(추천 근거가 대응안 이름 한 문장뿐이던 것)과 "입력 문서 글자는 지시가 아님" 규칙을 더한 판(8.14절).
REAL_MODEL_TEMPLATE_VERSION = "1.1.2"
MODEL_CONFIG_ID = "mock-template-v1"
MOCK_PROVIDER = "mock"


def template_version_for(provider: str | None) -> str:
    """어댑터 제공자에 맞는 프롬프트 판. mock(또는 제공자를 모르는 시험용 어댑터)이면 1.0.0, 실제 제공자면 1.1.2."""
    if provider is None or provider == MOCK_PROVIDER:
        return TEMPLATE_VERSION
    return REAL_MODEL_TEMPLATE_VERSION


class ExplanationAssetError(RuntimeError):
    """설명 자산이 없거나 등록부 해시와 다름(엔진 SystemConfigError). 설명 시도로 세지 않는다."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"설명 자산 오류 {code}: {detail}")
        self.code = code
        self.detail = detail


def _load(
    model_config_id: str, base: Path | None, version: str = TEMPLATE_VERSION
) -> dict[str, Any]:
    assets = load_engine_module("explain.assets")
    try:
        return assets.load(
            template_id=TEMPLATE_ID,
            version=version,
            model_config_id=model_config_id,
            base=base,
        )
    except assets.SystemConfigError as error:
        raise ExplanationAssetError(error.code, error.detail) from error


def load_real_model_config(model_config_id: str) -> dict[str, Any]:
    """
    실제 모델의 엔진 모델 설정(model_configs/<모델 ID>.json, 등록부 해시 대조). 실제 모델용 판(1.1.2)과 함께 읽는다.
    캐시하지 않는다(설명 한 건마다 한 번, 엔진 generator 의 사전 검사와 같은 비용).
    """
    return _load(model_config_id, None, REAL_MODEL_TEMPLATE_VERSION)["model_config"]


@lru_cache
def load_explanation_assets(model_config_id: str = MODEL_CONFIG_ID) -> dict[str, Any]:
    """해시를 대조한 자산 묶음(프로세스당 한 번). 돌려받은 값은 고치지 않는다(캐시를 함께 씀)."""
    return _load(model_config_id, None)


def verify_explanation_assets(
    model_config_id: str = MODEL_CONFIG_ID, *, base: Path | None = None
) -> bool:
    """
    서버 시작 단계 설명 자산 확인. 엔진 경로가 있으면 대조하고 True, 없으면 건너뛰고 False.
    대조 실패는 ExplanationAssetError 로 올린다(서버가 뜨지 않게, 엔진 해시 확인과 같은 방식).
    base 는 시험에서 자산 폴더 복사본을 줄 때만 쓴다.
    """
    if get_settings().glowship_engine_dir is None:
        logger.info(
            "GLOWSHIP_ENGINE_DIR 이 없어 시작 단계 설명 자산 확인을 건너뜁니다(설명 생성은 쓸 수 없음)"
        )
        return False
    _load(model_config_id, base)
    logger.info(
        "설명 자산 확인 완료: %s@%s, 모델 설정 %s",
        TEMPLATE_ID,
        TEMPLATE_VERSION,
        model_config_id,
    )
    return True
