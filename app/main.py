import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import Body, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolTimeout, TooManyRequests

from app.config import Settings
from app.database import make_pool, migrate
from app.models import EmptyBody, FixtureIn, TransferIn
from app.observability import Observability, RequestTelemetry, configure_logging
from app.service import DomainError, WalletService


def create_app(settings=None, failure_hook=None):
    settings = settings or Settings.from_env()
    configure_logging()
    obs = Observability()
    pool = make_pool(settings)
    service = WalletService(pool, settings, obs, failure_hook)

    @asynccontextmanager
    async def lifespan(app):
        await pool.open()
        try:
            await pool.wait(timeout=30)
            await migrate(pool)
            obs.emit("service_started")
            yield
        finally:
            await pool.close()

    app = FastAPI(
        title="Wallet & P2P Transfer",
        version="1.0.0",
        lifespan=lifespan,
        description="Assessment service. All amounts are integer paise. Synthetic funds only.",
    )
    app.state.service, app.state.pool, app.state.obs = service, pool, obs
    app.add_middleware(RequestTelemetry, obs=obs)
    bearer = HTTPBearer(auto_error=False)

    def get_token(credentials: HTTPAuthorizationCredentials | None):
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or len(credentials.credentials) > 256
        ):
            raise DomainError(401, "unauthorized", "Valid bearer token required")
        return credentials.credentials

    async def caller(credentials=Depends(bearer)):
        return await service.authenticate(get_token(credentials))

    async def admin(credentials=Depends(bearer)):
        token = get_token(credentials)
        if not secrets.compare_digest(token, settings.admin_token):
            raise DomainError(403, "forbidden", "Admin token required")

    def log_access():
        if not settings.public_logs:
            raise DomainError(404, "not_found", "Public logs disabled")

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo raw input, bearer tokens, or request bodies in errors/logs.
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Invalid request; see /docs for the schema",
                    "fields": [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()],
                }
            },
        )

    async def unavailable(request, exc):
        obs.emit("database_unavailable", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "1"},
            content={
                "error": {
                    "code": "temporarily_unavailable",
                    "message": "Outcome may be unknown; retry with the same idempotency key",
                }
            },
        )

    for exception in (OperationalError, InterfaceError, PoolTimeout, TooManyRequests):
        app.add_exception_handler(exception, unavailable)

    @app.get("/", tags=["operations"])
    async def index():
        return {
            "service": "wallet-p2p",
            "docs": "/docs",
            "health": "/healthz",
            "readiness": "/readyz",
            "metrics": "/metrics",
            "logs": "/logs",
            "events": "/events",
            "mode": "assessment-synthetic-money",
        }

    @app.get("/healthz", tags=["operations"])
    async def health():
        return {"status": "ok"}

    @app.get("/readyz", tags=["operations"])
    async def ready():
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
        return {"status": "ready"}

    @app.post("/wallets", tags=["wallets"])
    async def create_wallet(payload: EmptyBody | None = Body(default=None), user_id=Depends(caller)):
        return await service.get_or_create(user_id)

    @app.get("/wallets/{wallet_id}", tags=["wallets"])
    async def get_wallet(wallet_id: UUID, user_id=Depends(caller)):
        return await service.get_wallet(user_id, wallet_id)

    @app.post("/transfers", tags=["transfers"])
    async def transfer(payload: TransferIn, user_id=Depends(caller)):
        return await service.transfer(user_id, payload)

    @app.get("/transfers/{transfer_id}", tags=["transfers"])
    async def get_transfer(transfer_id: UUID, user_id=Depends(caller)):
        return await service.get_transfer(user_id, transfer_id)

    @app.post("/admin/fixtures", tags=["assessment fixtures"], dependencies=[Depends(admin)])
    async def create_fixtures(payload: FixtureIn):
        if not settings.enable_fixtures:
            raise DomainError(404, "not_found", "Fixtures disabled")
        return await service.fixtures(payload.count, payload.initial_balance_paise)

    @app.get("/metrics", tags=["operations"])
    async def metrics():
        return Response(generate_latest(obs.registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.get("/logs", tags=["operations"], dependencies=[Depends(log_access)])
    async def logs(limit: int = Query(100, ge=1, le=1000)):
        return {
            "instance_id": obs.instance_id,
            "retention": "latest 1000 records; resets on restart",
            "records": list(obs.tail)[-limit:],
        }

    @app.get("/events", tags=["operations"], dependencies=[Depends(log_access)])
    async def events(limit: int = Query(100, ge=1, le=1000)):
        return {"retention": "durable committed business events", "records": await service.events(limit)}

    @app.get("/logs/stream", tags=["operations"], dependencies=[Depends(log_access)])
    async def stream_logs(request: Request):
        async def stream():
            cursor, last_heartbeat = 0, 0
            while not await request.is_disconnected():
                for record in list(obs.tail):
                    if record["sequence"] > cursor:
                        cursor = record["sequence"]
                        yield "data: " + json.dumps(record) + "\n\n"
                last_heartbeat += 1
                if last_heartbeat >= 20:
                    yield ": heartbeat\n\n"
                    last_heartbeat = 0
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    return app
