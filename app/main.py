from fastapi import FastAPI

from app.core.config import get_settings
from app.core.exceptions import include_exception_handlers
from app.core.middleware import include_middlewares
from app.modules.dashboard.presentation.dashboard_api_router import (
    router as dashboard_router,
)
from app.modules.data_sync.presentation.data_sync_run_api_router import (
    router as data_sync_run_router,
)
from app.modules.decision.presentation.member_draft_api_router import (
    router as member_draft_router,
)
from app.modules.decision.presentation.reanalysis_api_router import (
    router as reanalysis_router,
)
from app.modules.delay_analysis.presentation.agent_dev_api_router import (
    router as agent_dev_router,
)
from app.modules.delay_analysis.presentation.analysis_run_api_router import (
    router as analysis_run_router,
)
from app.modules.delay_analysis.presentation.delay_event_api_router import (
    router as delay_event_router,
)
from app.modules.inventory.presentation.inventory_api_router import (
    router as inventory_router,
)
from app.modules.inventory.presentation.inventory_status_api_router import (
    router as inventory_status_router,
)
from app.modules.master_data.presentation.sku_api_router import router as sku_router
from app.modules.order.presentation.order_api_router import router as order_router
from app.modules.shipment.presentation.shipment_api_router import (
    router as shipment_router,
)
from app.modules.user.presentation.auth_api_router import router as auth_router
from app.modules.user.presentation.client_account_api_router import (
    router as client_account_router,
)
from app.modules.user.presentation.me_api_router import router as me_router

# 테이블 생성·변경은 Alembic 마이그레이션(make db-migrate)으로만 수행합니다.
app = FastAPI()

# Middleware
include_middlewares(app)

# Exceptions
include_exception_handlers(app)

# Routers
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(client_account_router)
app.include_router(dashboard_router)
app.include_router(data_sync_run_router)
app.include_router(delay_event_router)
app.include_router(analysis_run_router)
app.include_router(member_draft_router)
app.include_router(reanalysis_router)
app.include_router(inventory_router)
app.include_router(inventory_status_router)
app.include_router(order_router)
app.include_router(shipment_router)
app.include_router(sku_router)

# 개발 확인용 (운영 환경 제외)
if get_settings().app_env != "prod":
    app.include_router(agent_dev_router)
