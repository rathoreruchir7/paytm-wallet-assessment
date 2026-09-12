# Wallet & P2P Transfer - Paytm PML R2

Python 3.12 / FastAPI / PostgreSQL. A small backend focused on atomic money movement,
durable idempotency, and operational evidence. No frontend. All funds are synthetic.

**Publishing status:** source and deployment configuration are prepared. No live API,
public repository, or deployed logs URL has been created in this workspace yet.
See [the verification record](evidence/verification.md) for what has actually run.

**Executed:** 26 tests passed against real PostgreSQL; HTTP bursts and replay after
an actual API process restart also passed. Docker and live-host verification remain pending.

## Start with one command

Install Docker with Compose v2, then run from this folder:

```sh
docker compose up --build -d --wait
```

API: `http://localhost:8000` · OpenAPI: `http://localhost:8000/docs`

Compose starts the non-root app and PostgreSQL, waits for database health, and applies
versioned migrations. Database data survives app restarts in a named volume. The DB
has no host port; the API is bound to localhost. Development credentials in Compose
are intentionally local-only; deployment generates a different admin secret.

## Run the assessment probes

Requires only Python 3.12+ (no pip packages):

```sh
python scripts/burst.py
```

This creates isolated, funded fixtures through an admin-protected endpoint and asserts:

1. 50 get-or-create requests for a new user return one zero-balance wallet.
2. 50 identical concurrent transfers produce one debit/credit and identical JSON bodies.
3. Changed-body reuse returns 409 and leaves balances unchanged.
4. 400 contending transfers, including opposite directions, conserve total funds and
   reconcile every wallet against successful receipts.
5. 50 competing debits permit exactly one affordable transfer; the remainder decline.

Run individual cases with `--case wallets`, `--case idempotency`, `--case contention`,
or `--case overdraft`. Control load with `--concurrency`, `--requests`, and `--transfers`.
Each assertion failure exits nonzero. Successful probes print JSON evidence, never tokens.

The full local/CI gate builds the image, checks its user and HEALTHCHECK, runs the real
PostgreSQL integration tests, runs the burst, restarts the app, and replays a committed transfer:

```sh
python scripts/verify.py
```

Run just the tests with `docker compose --profile test run --build --rm test`.
GitHub Actions runs the same gate and retains logs, metrics, and the burst report.

## API and authentication

Every wallet/transfer request uses `Authorization: Bearer <user-token>`. Tokens contain
256 random bits; only SHA-256 hashes are stored. Tokens identify users, not wallets.
Only the source owner can debit; only the owner can read a wallet; either transfer
participant can read its receipt. The admin token cannot act as a wallet user.

| Endpoint | Request / response |
| --- | --- |
| `POST /wallets` | Empty body or `{}`; gets/creates the caller's zero-balance wallet, returns `id`, `balance_paise`. |
| `GET /wallets/{id}` | Returns the caller's current committed balance. |
| `POST /transfers` | `from`, `to`, positive integer `amount_paise`, `idempotency_key`; returns an immutable receipt. |
| `GET /transfers/{id}` | Returns the immutable transfer receipt to a participant. |
| `POST /admin/fixtures` | Admin bearer; `count` (2-10), `initial_balance_paise`. Returns funded users/tokens/wallets plus one new user without a wallet. |
| `GET /healthz`, `/readyz` | Process liveness / database readiness. |
| `GET /metrics` | Prometheus request, latency histogram, and domain counters. |
| `GET /logs`, `/logs/stream` | Sanitized JSON tail / SSE stream when `PUBLIC_LOGS=true`. |
| `GET /events` | Durable committed business events when `PUBLIC_LOGS=true`. |

Example transfer body:

```json
{
  "from": "11111111-1111-4111-8111-111111111111",
  "to": "22222222-2222-4222-8222-222222222222",
  "amount_paise": 1500,
  "idempotency_key": "order-2026-001"
}
```

Replace these illustrative UUIDs with real IDs from the fixture response. Obtain fixtures locally:

```sh
curl -s http://localhost:8000/admin/fixtures \
  -H 'Authorization: Bearer local-assessment-admin-token-change-before-deploy' \
  -H 'Content-Type: application/json' \
  -d '{"count":4,"initial_balance_paise":10000}'
```

Fixture creation is the only funding operation; it is explicitly outside the transfer
invariant. A new fixture call creates new users and new opening money, so finish setup
before taking the baseline. Normal POST /wallets never mints money or accepts an opening
balance. Fixture tokens appear only in that admin response; keep them private.

### Response semantics

- HTTP 200 for both newly finalized transfers and replays, including business declines.
  `status` is `succeeded` or `declined`; inspect `reason` for insufficient funds or a
  destination balance limit. An HTTP 200 decline is not a successful payment.
- A declined key remains declined after later funding. Use a new key for a new attempt.
- Identical means the status code and JSON receipt body; correlation IDs and transport
  headers legitimately differ per request. Current balances are never embedded in receipts.
- Idempotency is scoped to `(authenticated user, key)`, not global across all customers.
  UUIDs are normalized and JSON field order is ignored. Different valid body: HTTP 409.
- Malformed input is 422 before idempotency processing. Missing wallets (404) and
  ownership errors (403) roll back the reservation and do not create transfer records.
- Reject floats (including `1.0`), booleans, strings, nonpositive amounts, self-transfers,
  unknown fields, and values above `9,007,199,254,740,991` paise. BIGINT stores amounts;
  the documented cap also preserves exact JSON integers in JavaScript clients.
- On DB failure, lock timeout, or capacity exhaustion, return 503 with Retry-After.
  A connection failure during commit can have an unknown outcome. Retry the **same key**;
  never infer that an HTTP error means the debit did not commit.

## Correctness and design

Read [the one-page write-up](docs/writeup.md) and [the operating guide](docs/operations.md).

`WalletService.transfer` in `app/service.py` contains the entire money transaction.
PostgreSQL enforces wallet/user and idempotency uniqueness. A separate idempotency
reservation table avoids wallet FK locks before the explicit sorted lock sequence.
Its deferred FK makes a key without a completed transfer uncommittable.

All balance reads and writes go to the same PostgreSQL primary. There is no cache,
process mutex, background transfer worker, or dependency on one API process for correctness.
The app uses one worker for a small free tier and process-local telemetry.

## Deploy the actual Docker image

Follow [DEPLOY.md](DEPLOY.md): publish the source, create a Render Blueprint from
`render.yaml`, wait for deployment, then run the burst against the assigned HTTPS URL.
The Blueprint selects a **Docker runtime** and free managed PostgreSQL; Render builds
and runs this Dockerfile. It does not deploy a Python buildpack or an in-memory substitute.

In bash, set `ADMIN_TOKEN` privately and run:

```sh
python scripts/burst.py --base-url https://YOUR-ASSIGNED-HOST --report evidence/live-burst.json
```

In PowerShell, set `$env:ADMIN_TOKEN` first and use the same Python command. Use the
assigned URL from Render, not the example above. Do not publish the admin token.

## AI disclosure

The human supplied the exercise and asked for a working backend. AI chose the stack,
locking strategy, idempotency schema, fixture workflow, tests, and deployment packaging,
and generated the implementation and documentation. No claim is made that the human
independently chose, reviewed, tested, or deployed these decisions. Update the disclosure
only for work and review you actually perform before submitting.
