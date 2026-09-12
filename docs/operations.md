# Operate and explain the service

## Signals

The app emits JSON to stdout. Each HTTP request gets a fresh correlation ID, or accepts
a validated `X-Correlation-ID`. Wallet and transfer routes emit `request_completed` with
the route template, response status, and duration. Unmatched paths use a fixed label to
avoid unbounded metric cardinality. No token, request body, query string, IP, or username
is logged. Generic errors expose the exception type, not database details.

Transfer events: `transfer_created`, `debited`, `credited`, `declined`,
`idempotent_replay_hit`, and `idempotency_conflict`. A decline has `insufficient_funds`
or `destination_limit`. Business-event rows are inserted in the money transaction,
so committed history survives process death. Stdout emission and in-process counters
happen after commit; a crash between commit and emission can omit those operational signals.
The durable `/events` records and transfer table remain the source of truth.

`/logs` exposes the latest 1,000 sanitized process records; `/logs/stream` uses SSE.
This is a bounded assessment tail, not permanent log aggregation. An `instance_id` changes
on restart. `/events` shows the latest durable events (ordered by identity, which is not
a strict commit-time order across transactions). Set `PUBLIC_LOGS=false` to disable all
three public log endpoints outside this synthetic assessment.

## Prometheus queries

Scrape `/metrics` every 15 seconds if a Prometheus instance is available. A paid dashboard
is unnecessary. Counters and histograms are per process and reset on restart; Prometheus
`rate` handles counter resets. Use a single Uvicorn worker as configured, or scrape each
instance separately when scaling. Exact persistent counts can be queried from `transfers`.
Readiness/liveness, log streams, event reads, and scrapes are excluded from HTTP business
traffic metrics, so long-lived streams and probes do not distort latency or throughput.

Request rate:

```promql
sum(rate(wallet_http_requests_total[5m]))
```

p99 latency by route (seconds):

```promql
histogram_quantile(0.99, sum by (le, route) (rate(wallet_http_request_duration_seconds_bucket[5m])))
```

Server error ratio (0 to 1):

```promql
sum(rate(wallet_http_requests_total{status=~"5.."}[5m]))
/
clamp_min(sum(rate(wallet_http_requests_total[5m])), 0.000001)
```

Use `status=~"4.."` separately for client errors. Declined transfers return 200, so also watch:

```promql
rate(wallet_transfers_created_total[5m])
rate(wallet_transfers_declined_total{reason="insufficient_funds"}[5m])
rate(wallet_idempotent_replays_total[5m])
rate(wallet_idempotency_conflicts_total[5m])
```

Histograms estimate p99; low-traffic windows may have too few samples. The burst report
also includes a client-side p99; that measures a different path, including network time.

## Failure behavior

| Failure | Behavior / recovery |
| --- | --- |
| Concurrent debit attempts | Sorted locks serialize overlapping wallet pairs; only affordable debits succeed. |
| Same key while original is running | Unique insert waits for the original commit/rollback; then replays or becomes the first attempt. |
| Crash after debit, before credit/commit | PostgreSQL rolls back both the provisional debit and key reservation. Retry same key. |
| Commit succeeds but response is lost | Retry same key returns the stored receipt without moving money again. |
| Connection lost during commit | Outcome is uncertain to the client; return/retry with same key. Never generate a fresh retry key. |
| DB outage or lock timeout | Fail with 503 rather than accept an uncommitted payment. Bounded pool waiting protects capacity. |
| Process restart | Balances/keys/transfers/events persist; in-memory logs and counters reset. |
| DB permanently lost | Free hosting has no promised recovery. A real money deployment requires tested backups and failover. |

`scripts/verify.py` runs the real Docker image and validates restart replay. Integration
tests inject failures through a constructor-only hook; no public failure-trigger endpoint
exists. `scripts/reconcile.sql` independently checks total funds and each wallet against
opening balances plus successful incoming/outgoing receipts in one consistent snapshot.

## Boundaries and deliberate omissions

This assessment is not a regulated, production payment platform. It has no external
payment rails, settlement, refunds, KYC, customer onboarding, fraud controls, or complete
accounting ledger. Database credentials can alter tables directly; invariants assume
money changes go through the service's transaction path. A production evolution would
separate migration/runtime DB roles, use an append-only double-entry ledger, add admission
control, secret rotation, backup drills, alerting, and outbox delivery to an external log sink.

One PostgreSQL primary is the consistency boundary. There are no read replicas or cached
balances. Sequential GETs are individually committed views, not a common snapshot during
ongoing transfers; measure conservation after a burst quiesces or use the reconciliation
query's snapshot. Multi-node availability during a database partition is deliberately
sacrificed. Synchronous commit means the configured primary acknowledges its WAL commit;
it is not a claim of synchronous geographic replication or zero-loss failover.

## Live interview walkthrough (about five minutes)

1. Open `/docs`; explain integer paise, bearer identity, and immutable receipt responses.
2. Start `curl -N <URL>/logs/stream`; run `scripts/burst.py` against the deployed URL.
3. Show identical retry results and receipt-based per-wallet reconciliation.
4. Open the transaction in `app/service.py`; trace reservation, sorted locks, debit,
   credit, receipt, durable events, and commit.
5. Explain the lock-order/FK trap, the lost-response test, the free-tier trade-off, and
   what AI designed. Cite verified evidence rather than claiming unrun tests passed.
