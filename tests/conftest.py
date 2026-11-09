"""Test stack: real PostgreSQL, MongoDB and Redis in containers; LLM_PROVIDER=fake; TASKS_EAGER=true.

The schema is created with `alembic upgrade head`, so the migrations are exercised too.
Each test starts from empty databases plus the demo reference data (rubric, schools, courses, users).
"""

import os
import uuid
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
import sqlalchemy as sa
from testcontainers.community.mongodb import MongoDbContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer


@pytest.fixture(scope="session")
def containers(tmp_path_factory):
    with (
        PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg,
        MongoDbContainer("mongo:7") as mongo,
        RedisContainer("redis:7-alpine") as redis,
    ):
        os.environ.update(
            ENV="test",
            AUTH_PROVIDER="stub",
            DATABASE_URL=pg.get_connection_url(),
            MONGO_URL=mongo.get_connection_url(),
            MONGO_DB="syllabus_test",
            REDIS_URL=f"redis://{redis.get_container_host_ip()}:{redis.get_exposed_port(6379)}/0",
            TASKS_EAGER="true",
            LLM_PROVIDER="fake",
            EMAIL_BACKEND="console",
            FILES_ROOT=str(tmp_path_factory.mktemp("files")),
            TMP_ROOT=str(tmp_path_factory.mktemp("tmp")),
            DRIVE_PROVIDER="stub",
            DRIVE_STUB_ROOT=str(tmp_path_factory.mktemp("drive")),
            SECRETS_ROOT=str(tmp_path_factory.mktemp("secrets")),
            GOOGLE_IMPERSONATE_USER="",
            DRIVE_DEFAULT_FOLDER_ID="",
            # Pinned so a developer .env (e.g. switched to real data) cannot change test behaviour.
            DRIVE_READ_ONLY="false",
        )
        from app.config import get_settings

        get_settings.cache_clear()

        from alembic import command
        from alembic.config import Config

        command.upgrade(Config("alembic.ini"), "head")
        yield


@pytest.fixture(scope="session")
async def stack(containers) -> AsyncIterator[None]:
    from app.db import pg
    from app.db.mongo import close_mongo, init_mongo
    from app.storage import cache

    pg.configure(pg.make_engine())
    await init_mongo()
    yield
    await close_mongo()
    await cache.close()
    await pg.get_engine().dispose()


@pytest.fixture
async def ref(stack) -> dict:
    """Clean databases + reference data. Returns slug → model maps (schools, programs, users, courses, folders)."""
    from app.db import pg
    from app.documents import DOCUMENT_MODELS
    from app.models import Base
    from app.storage import cache
    from seed.demo import _reference_data

    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with pg.get_engine().begin() as conn:
        await conn.execute(sa.text(f"TRUNCATE {tables} CASCADE"))
    for model in DOCUMENT_MODELS:
        await model.get_pymongo_collection().delete_many({})
    await cache.client().flushdb()
    from app.storage import drive_config

    drive_config.forget()
    async with pg.session_scope() as session:
        return await _reference_data(session)


@pytest.fixture
async def client_for(stack) -> AsyncIterator[Callable[[object], httpx.AsyncClient]]:
    """client_for(user) → an HTTP client authenticated as that user (stub bearer token)."""
    from app.main import create_app

    app = create_app(use_lifespan=False)
    clients: list[httpx.AsyncClient] = []

    def make(user) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/api/v1",
            headers={"Authorization": f"Bearer stub:{user.email}"},
        )
        clients.append(client)
        return client

    yield make
    for c in clients:
        await c.aclose()


@pytest.fixture
def anon(stack) -> httpx.AsyncClient:
    from app.main import create_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(use_lifespan=False)), base_url="http://test/api/v1"
    )


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def create_syllabus(
    client: httpx.AsyncClient, course_id: uuid.UUID, semester: str = "Fall 2026"
) -> dict:
    response = await client.post("/syllabi", json={"course_id": str(course_id), "semester": semester})
    assert response.status_code == 201, response.text
    return response.json()


async def upload(
    client: httpx.AsyncClient, syllabus_id: str, data: bytes, name: str = "syllabus.docx"
) -> dict:
    response = await client.post(f"/syllabi/{syllabus_id}/versions", files={"file": (name, data, DOCX)})
    assert response.status_code == 201, response.text
    return response.json()
