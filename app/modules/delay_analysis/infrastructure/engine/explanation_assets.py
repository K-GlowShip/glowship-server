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
      - 모델 설정은 mock(mock-template-v1)만 쓴다. 실제 외부 모델 호출은 아직 승인되지 않았다(엔진 adapter.py).
"""

logger = logging.getLogger(__name__)

TEMPLATE_ID = "explain_ready_v1"
TEMPLATE_VERSION = "1.0.0"
MODEL_CONFIG_ID = "mock-template-v1"


class ExplanationAssetError(RuntimeError):
    """설명 자산이 없거나 등록부 해시와 다름(엔진 SystemConfigError). 설명 시도로 세지 않는다."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"설명 자산 오류 {code}: {detail}")
        self.code = code
        self.detail = detail


def _load(model_config_id: str, base: Path | None) -> dict[str, Any]:
    assets = load_engine_module("explain.assets")
    try:
        return assets.load(
            template_id=TEMPLATE_ID,
            version=TEMPLATE_VERSION,
            model_config_id=model_config_id,
            base=base,
        )
    except assets.SystemConfigError as error:
        raise ExplanationAssetError(error.code, error.detail) from error


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
