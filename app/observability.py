import json
import logging
import re
import time
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import uuid4

from prometheus_client import CollectorRegistry, Counter, Histogram

correlation_id = ContextVar("correlation_id", default="startup")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Do not emit exception strings: DB errors may contain URLs or row values.
        return json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname.lower(),
                "event": record.getMessage(),
                "correlation_id": correlation_id.get(),
            }
        )


def configure_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    # Psycopg connection-error messages may contain hostnames/usernames.
    logging.getLogger("psycopg.pool").setLevel(logging.CRITICAL)


class Observability:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "wallet_http_requests_total",
            "HTTP responses",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "wallet_http_request_duration_seconds",
            "HTTP latency",
            ["method", "route"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 60),
            registry=self.registry,
        )
        self.created = Counter(
            "wallet_transfers_created_total", "Committed transfer records", registry=self.registry
        )
        self.declined = Counter(
            "wallet_transfers_declined_total", "Committed declines", ["reason"], registry=self.registry
        )
        for reason in ("insufficient_funds", "destination_limit"):
            self.declined.labels(reason)
        self.replays = Counter(
            "wallet_idempotent_replays_total", "Same-body replay hits", registry=self.registry
        )
        self.conflicts = Counter(
            "wallet_idempotency_conflicts_total", "Different-body key reuse", registry=self.registry
        )
        self.tail = deque(maxlen=1000)
        self.sequence = 0
        self.instance_id = str(uuid4())

    def emit(self, event, **fields):
        self.sequence += 1
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": "info",
            "event": event,
            "correlation_id": correlation_id.get(),
            "sequence": self.sequence,
            "instance_id": self.instance_id,
            **fields,
        }
        # Only explicit allowlisted metadata enters this method; no tokens/bodies/IPs.
        print(json.dumps(record, separators=(",", ":")), flush=True)
        self.tail.append(record)


class RequestTelemetry:
    def __init__(self, app, obs):
        self.app, self.obs = app, obs

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        supplied = headers.get(b"x-correlation-id", b"").decode("ascii", errors="ignore")
        cid = supplied if re.fullmatch(r"[A-Za-z0-9._:-]{1,64}", supplied) else str(uuid4())
        ctx = correlation_id.set(cid)
        started, status = time.perf_counter(), 500
        sent = False

        async def send_with_id(message):
            nonlocal status, sent
            if message["type"] == "http.response.start":
                sent = True
                status = message["status"]
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-correlation-id", cid.encode()),
                    (b"cache-control", b"no-store"),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            self.obs.emit("request_failed", error_type=type(exc).__name__)
            if sent:
                raise
            payload = b'{"error":{"code":"internal_error","message":"Retry with the same idempotency key"}}'
            await send_with_id(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": payload})
        finally:
            elapsed = time.perf_counter() - started
            route = getattr(scope.get("route"), "path", "unmatched")
            if route not in ("/metrics", "/logs", "/logs/stream", "/healthz", "/readyz", "/events"):
                self.obs.requests.labels(scope["method"], route, str(status)).inc()
                self.obs.latency.labels(scope["method"], route).observe(elapsed)
                self.obs.emit(
                    "request_completed",
                    method=scope["method"],
                    route=route,
                    status=status,
                    duration_ms=round(elapsed * 1000, 3),
                )
            correlation_id.reset(ctx)
