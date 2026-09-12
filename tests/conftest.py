import os

import httpx
import pytest

from app.config import Settings
from app.main import create_app

ADMIN_TOKEN = "integration-test-admin-token-with-32-characters"


@pytest.fixture(scope="module")
async def running():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip(
            "Real PostgreSQL required: set TEST_DATABASE_URL or run docker compose --profile test run --build --rm test"
        )
    app = create_app(
        Settings(
            dsn,
            ADMIN_TOKEN,
            enable_fixtures=True,
            public_logs=True,
            pool_timeout_seconds=float(os.getenv("TEST_POOL_TIMEOUT_SECONDS", "30")),
            connect_timeout_seconds=int(os.getenv("TEST_CONNECT_TIMEOUT_SECONDS", "10")),
            lock_timeout_ms=int(os.getenv("TEST_LOCK_TIMEOUT_MS", "5000")),
            statement_timeout_ms=int(os.getenv("TEST_STATEMENT_TIMEOUT_MS", "10000")),
        )
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield app, client


def auth(token):
    return {"Authorization": "Bearer " + token}


async def fixture_users(client, count=4, amount=10_000):
    result = await client.post(
        "/admin/fixtures", headers=auth(ADMIN_TOKEN), json={"count": count, "initial_balance_paise": amount}
    )
    assert result.status_code == 200, result.text
    return result.json()
