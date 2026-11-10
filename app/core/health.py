import asyncio
import logging

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core import database

"""
    헬스 엔드포인트 (배포 확인용, 인증 없음)

    GET /health
      - 서버 프로세스가 요청을 받고, 앱 DB 엔진으로 SELECT 1 이 되면 200 {"status": "ok", "database": "ok"}.
      - DB 에 붙지 못하거나 응답이 DB_TIMEOUT_SECONDS 를 넘으면 503 {"status": "error", "database": "error"}.
        배포 직후 가장 흔한 실패(설정 파일의 RDS 주소·계정 오류, 보안 그룹)가 바로 드러나게 하려는 것이다.
      - 배포 스크립트(scripts/deploy/deploy-dev.sh)가 컨테이너 교체 뒤 이 경로가 200 이 될 때까지 기다리고,
        안 되면 직전 이미지로 되돌린다. /api/v1 아래가 아니라 프론트 API 목록(docs/frontend-api.md)에는 넣지 않는다.
      - 실패 사유는 응답에 넣지 않고 로그에만 남긴다(외부에 DB 접속 정보 모양을 노출하지 않기 위해).
"""

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

DB_TIMEOUT_SECONDS = 3.0


async def _check_database() -> bool:
    """앱 DB 엔진으로 SELECT 1. 성공이면 True, 접속 실패·시간 초과면 False."""
    try:
        async with asyncio.timeout(DB_TIMEOUT_SECONDS):
            async with database.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        return True
    except Exception as error:  # noqa: BLE001 - 어떤 실패든 503 으로 바꾸고 사유는 로그에만 남긴다
        logger.warning("헬스 확인: DB 접속 실패 (%s)", type(error).__name__)
        return False


@router.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    database_ok = await _check_database()
    if database_ok:
        return JSONResponse({"status": "ok", "database": "ok"})
    return JSONResponse(
        {"status": "error", "database": "error"},
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )
