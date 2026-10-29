from fastapi import FastAPI

from app.core.config import get_settings
from app.core.exceptions import include_exception_handlers
from app.core.middleware import include_middlewares
from app.modules.delay_analysis.presentation.agent_dev_api_router import (
    router as agent_dev_router,
)
from app.modules.user.presentation.auth_api_router import router as auth_router

# 테이블 생성·변경은 Alembic 마이그레이션(make db-migrate)으로만 수행합니다.
app = FastAPI()

# Middleware
include_middlewares(app)

# Exceptions
include_exception_handlers(app)

# Routers
app.include_router(auth_router)

# 개발 확인용 (운영 환경 제외)
if get_settings().app_env != "prod":
    app.include_router(agent_dev_router)
