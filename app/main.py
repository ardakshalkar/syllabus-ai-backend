from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import get_settings
from app.db import pg
from app.db.mongo import close_mongo, init_mongo
from app.storage import cache


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    pg.get_engine()
    await init_mongo()
    yield
    await close_mongo()
    await cache.close()
    await pg.get_engine().dispose()


def create_app(*, use_lifespan: bool = True) -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Syllabus AI API",
        version="0.1.0",
        description="Narxoz University syllabus quality management: AI checks, approval workflow, sessions, analytics.",
        lifespan=lifespan if use_lifespan else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix=settings.api_prefix)

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        async with pg.sessionmaker()() as session:
            await session.execute(sa.text("select 1"))
        await cache.client().ping()
        return {"status": "ok"}

    return app


app = create_app()
