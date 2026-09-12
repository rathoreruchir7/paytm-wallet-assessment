import os
from dataclasses import dataclass

MAX_PAISE = 9_007_199_254_740_991  # JSON-safe integer; PostgreSQL storage is BIGINT.


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_token: str
    enable_fixtures: bool = False
    public_logs: bool = False
    pool_size: int = 10
    pool_timeout_seconds: float = 30
    connect_timeout_seconds: int = 10
    lock_timeout_ms: int = 5000
    statement_timeout_ms: int = 10000

    @classmethod
    def from_env(cls):
        token = os.environ.get("ADMIN_TOKEN", "")
        if len(token) < 32:
            raise ValueError("ADMIN_TOKEN must contain at least 32 characters")
        return cls(
            database_url=os.environ["DATABASE_URL"],
            admin_token=token,
            enable_fixtures=os.getenv("ENABLE_FIXTURES", "false").lower() == "true",
            public_logs=os.getenv("PUBLIC_LOGS", "false").lower() == "true",
            pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
            pool_timeout_seconds=float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "30")),
            connect_timeout_seconds=int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "10")),
        )
