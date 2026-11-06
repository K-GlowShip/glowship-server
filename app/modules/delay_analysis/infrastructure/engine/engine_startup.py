import logging

from app.core.config import get_settings
from app.modules.delay_analysis.infrastructure.engine.engine_loader import (
    load_verified_engine_dir,
)

"""
    서버 시작 때 엔진 확인 (docs/analysis/engine-local-path.md 6절)
      - GLOWSHIP_ENGINE_DIR 이 설정돼 있으면 시작 단계에서 해시 대조를 한 번 해서, 원본이 없거나 다르면 서버가 뜨지 않게 합니다.
        누락·판 어긋남이 첫 분석 요청 때가 아니라 배포 직후에 드러나게 하려는 것입니다(PR #25 리뷰).
      - 설정하지 않았으면 건너뜁니다. 엔진이 없는 PC·CI 에서도 서버와 다른 기능은 돌아야 하기 때문입니다.
        이때 엔진이 필요한 기능은 처음 부를 때 EngineNotConfiguredError 를 냅니다.
      - 대조 결과는 load_verified_engine_dir 의 캐시에 남으므로, 분석 때 다시 대조하지 않습니다.
"""

logger = logging.getLogger(__name__)


def verify_engine_on_startup() -> bool:
    """엔진 경로가 설정돼 있으면 대조하고 True. 설정이 없으면 건너뛰고 False. 대조 실패는 예외를 그대로 올린다."""
    if get_settings().glowship_engine_dir is None:
        logger.info(
            "GLOWSHIP_ENGINE_DIR 이 없어 시작 단계 엔진 확인을 건너뜁니다(엔진이 필요한 기능은 쓸 수 없음)"
        )
        return False
    engine_dir = load_verified_engine_dir()
    logger.info("엔진 확인 완료: %s", engine_dir)
    return True
