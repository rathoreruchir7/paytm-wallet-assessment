# Verification record

This record distinguishes executed checks from prepared but unexecuted deployment gates.

## Executed successfully

| Check | Result | Evidence |
| --- | --- | --- |
| Ruff lint / formatting | Passed; 14 Python files formatted | Build tool execution |
| Validation and real PostgreSQL integration suite | **26 passed**, 78.38 seconds | `pytest.xml` |
| Concurrent get-or-create | 50 HTTP requests, one wallet | `http-burst.json` |
| Same-key retry storm | 50 HTTP requests, one debit/credit of 137 paise | `http-burst.json` |
| Changed-body replay | HTTP 409; balances unchanged | `http-burst.json` |
| Contention | 100 successful HTTP transfers; total 40,000 -> 40,000 paise | `http-burst.json` |
| Per-wallet reconciliation | Receipts reconcile; minimum balance 9,382 paise | `http-burst.json` |
| Overdraft burst | 50 attempts: one success, 49 insufficient-funds declines | `http-burst.json` |
| API process restart | New OS process; identical receipt, no second debit | `restart.json` |
| Durable business events | All three transfer events survived restart | `restart.json` |
| Streaming logs | SSE record received with a correlation ID | `restart.json` |
| Metrics endpoint | Prometheus response captured after restart | `metrics-after-restart.prom` |
| One-page write-up | PDF rendered, visually inspected, confirmed as one page | `../docs/writeup.pdf` |

The integration suite also covers simultaneous different-body key races,
opposite-direction transfers with database snapshots during contention, declined
replay after funding, ownership/participant authorization, per-user key scope,
destination overflow, database constraints, rollback after debit/before commit,
and a simulated lost response after a successful commit.

## Test environment and limits

The database was a **real temporary managed PostgreSQL 17.11 primary on Neon**,
not SQLite, an emulator, or a mocked repository. The environment lacks native
PostgreSQL networking, so a private test-only loopback relay used Neon's documented
secure WebSocket protocol through the configured network proxy. It forwards the
PostgreSQL wire protocol; it does not emulate SQL, locks, sessions, or commits.
Credentials and the relay's private files are excluded from this project.

The integration suite used a 30-second connection timeout, 180-second pool wait,
30-second lock timeout, and 45-second statement timeout to accommodate that transport.
Application defaults remain a 10-second connection timeout, 30-second pool wait,
5-second lock timeout, and 10-second statement timeout. An early run exposed timeouts;
the final implementation batches transaction settings and post-debit writes to reduce
round trips and avoids unnecessary wallet writes on get-or-create replays.

The black-box HTTP burst used **8 concurrent clients**, 50 requests per race/storm/
overdraft probe, and 100 contention transfers. The API used a 30-second connection
timeout and 60-second pool wait; its normal 5-second lock and 10-second statement
timeouts were retained. Client p50 was 1,079.05 ms and p99 was 1,799.23 ms. These are
test-transport measurements, not production throughput or free-host performance claims.
The separate integration tests launch 50 same-key requests concurrently and 200
contending requests through the ASGI API against the actual database.

The checked-in burst defaults to 50 concurrent clients and 400 contention transfers.
That default-size black-box run still needs to be executed on the deployed host.

## Not yet executed / not yet created

- Docker image build, HEALTHCHECK/non-root inspection, and complete Compose gate:
  no Docker daemon is available here. `python scripts/verify.py` and GitHub Actions are prepared.
- PostgreSQL 16 container test: Compose/Render select PostgreSQL 16; the executed remote
  tests used PostgreSQL 17.11. No PostgreSQL 16 compatibility result is claimed yet.
- Public GitHub repository, Render deployment, public API/logs links, and live-host burst:
  GitHub and Render account connections are not available to this session.
- Permanent hosting of the temporary test database: it is a test fixture, not the
  assessment's submitted database or a promised long-lived service.

**The source has been exercised, but the assessment is not ready to submit until
the Docker/CI and live deployment checks above are complete.**

Reproduction: [DEPLOY.md](../DEPLOY.md), `python scripts/verify.py`, and
`python scripts/burst.py --base-url <actual-live-url> --report evidence/live-burst.json`.
