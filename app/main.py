from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.database import engine
from app.core.exceptions import include_exception_handlers
from app.core.middleware import include_middlewares
from app.core.orm import Base
from app.modules.user.presentation.user_api_router import router as user_router


# if: build -> DB create and drop
@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


app = FastAPI(lifespan=lifespan)

# Middleware
include_middlewares(app)

# Exceptions
include_exception_handlers(app)

# Routers
app.include_router(user_router)
