# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY requirements.lock .
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.lock

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
RUN groupadd --gid 10001 wallet && useradd --uid 10001 --gid wallet --no-create-home wallet
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=wallet:wallet app ./app
COPY --chown=wallet:wallet migrations ./migrations
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/healthz',timeout=3)"
CMD ["python", "-m", "app.run"]

FROM runtime AS test
USER root
COPY requirements-dev.lock requirements.lock ./
RUN pip install --no-cache-dir -r requirements-dev.lock
COPY --chown=wallet:wallet tests ./tests
COPY --chown=wallet:wallet scripts ./scripts
COPY --chown=wallet:wallet pyproject.toml ./
USER 10001:10001
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# Unqualified builds (including Render) produce the small runtime image.
FROM runtime AS production
