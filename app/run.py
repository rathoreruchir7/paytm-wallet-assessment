import os

import uvicorn

from app.observability import configure_logging

if __name__ == "__main__":
    configure_logging()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        workers=1,
        access_log=False,
        log_config=None,
        limit_concurrency=256,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=20,
        proxy_headers=False,
    )
