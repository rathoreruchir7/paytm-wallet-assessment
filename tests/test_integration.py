import asyncio
from uuid import UUID, uuid4

import pytest
from psycopg import errors

from app.config import MAX_PAISE
from tests.conftest import ADMIN_TOKEN, auth, fixture_users


def body(a, b, amount=100, key=None):
    return {
        "from": a["wallet_id"],
        "to": b["wallet_id"],
        "amount_paise": amount,
        "idempotency_key": key or str(uuid4()),
    }


async def balance(client, user):
    r = await client.get("/wallets/" + user["wallet_id"], headers=auth(user["token"]))
    assert r.status_code == 200
    return r.json()["balance_paise"]


async def test_concurrent_get_or_create_has_one_database_row(running):
    app, client = running
    fresh = (await fixture_users(client))["fresh_user"]
    responses = await asyncio.gather(
        *[client.post("/wallets", headers=auth(fresh["token"])) for _ in range(50)]
    )
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 1
    assert all(r.json()["balance_paise"] == 0 for r in responses)
    async with app.state.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) AS n FROM wallets WHERE user_id=%s", (UUID(fresh["user_id"]),)
            )
        ).fetchone()
    assert row["n"] == 1


async def test_same_key_storm_and_different_body_conflict(running):
    app, client = running
    a, b = (await fixture_users(client, 2))["users"]
    payload = body(a, b, 137)
    responses = await asyncio.gather(
        *[client.post("/transfers", json=payload, headers=auth(a["token"])) for _ in range(50)]
    )
    assert all(r.status_code == 200 for r in responses)
    assert len({r.content for r in responses}) == 1
    assert await balance(client, a) == 9863 and await balance(client, b) == 10137
    conflict = await client.post(
        "/transfers", json={**payload, "amount_paise": 138}, headers=auth(a["token"])
    )
    assert conflict.status_code == 409
    assert await balance(client, a) == 9863
    async with app.state.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) AS n FROM transfers WHERE user_id=%s AND idempotency_key=%s",
                (UUID(a["user_id"]), payload["idempotency_key"]),
            )
        ).fetchone()
    assert row["n"] == 1


async def test_opposite_directions_reconcile_and_conserve_during_contention(running):
    app, client = running
    users = (await fixture_users(client))["users"]
    ids = [UUID(u["wallet_id"]) for u in users]
    observed = []
    stop = asyncio.Event()

    async def observe():
        while not stop.is_set():
            async with app.state.pool.connection() as conn:
                row = await (
                    await conn.execute(
                        """SELECT sum(balance_paise) AS total,
                    min(balance_paise) AS lowest FROM wallets WHERE id = ANY(%s)""",
                        (ids,),
                    )
                ).fetchone()
            observed.append(row)
            await asyncio.sleep(0.002)

    observer = asyncio.create_task(observe())
    payloads = []
    for i in range(200):
        a, b = (users[i % 4], users[(i + 1) % 4]) if i % 2 else (users[1], users[0])
        payloads.append((a, body(a, b, 37)))
    try:
        responses = await asyncio.gather(
            *[client.post("/transfers", json=p, headers=auth(a["token"])) for a, p in payloads]
        )
    finally:
        stop.set()
        await observer
    assert all(r.status_code == 200 for r in responses), [
        (r.status_code, r.text) for r in responses if r.status_code != 200
    ]
    assert observed and all(r["total"] == 40000 and r["lowest"] >= 0 for r in observed)
    expected = {u["wallet_id"]: 10000 for u in users}
    for r in responses:
        data = r.json()
        if data["status"] == "succeeded":
            expected[data["from"]] -= data["amount_paise"]
            expected[data["to"]] += data["amount_paise"]
    for user in users:
        assert await balance(client, user) == expected[user["wallet_id"]]


async def test_concurrent_debits_never_overdraw(running):
    _, client = running
    a, b = (await fixture_users(client, 2, 1000))["users"]
    responses = await asyncio.gather(
        *[client.post("/transfers", json=body(a, b, 600), headers=auth(a["token"])) for _ in range(40)]
    )
    assert all(r.status_code == 200 for r in responses)
    assert sum(r.json()["status"] == "succeeded" for r in responses) == 1
    assert sum(r.json()["reason"] == "insufficient_funds" for r in responses) == 39
    assert await balance(client, a) == 400 and await balance(client, b) == 1600


async def test_decline_replayed_even_after_funding(running):
    _, client = running
    a, b = (await fixture_users(client, 2, 100))["users"]
    payload = body(a, b, 101)
    first = await client.post("/transfers", json=payload, headers=auth(a["token"]))
    assert first.json()["reason"] == "insufficient_funds"
    assert (await client.post("/transfers", json=body(b, a, 50), headers=auth(b["token"]))).json()[
        "status"
    ] == "succeeded"
    replay = await client.post("/transfers", json=payload, headers=auth(a["token"]))
    assert replay.content == first.content
    assert await balance(client, a) == 150


@pytest.mark.parametrize("stage", ["after_debit", "before_commit"])
async def test_exception_rolls_back_money_key_and_events(running, stage):
    app, client = running
    a, b = (await fixture_users(client, 2))["users"]
    payload = body(a, b)

    async def fail(at, conn):
        if at == stage:
            raise RuntimeError("simulated failure")

    app.state.service.failure_hook = fail
    result = await client.post("/transfers", json=payload, headers=auth(a["token"]))
    assert result.status_code == 500
    assert await balance(client, a) == await balance(client, b) == 10000
    async with app.state.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT count(*) AS n FROM idempotency_keys WHERE user_id=%s AND key=%s",
                (UUID(a["user_id"]), payload["idempotency_key"]),
            )
        ).fetchone()
    assert row["n"] == 0
    app.state.service.failure_hook = None
    result = await client.post("/transfers", json=payload, headers=auth(a["token"]))
    assert result.status_code == 200 and result.json()["status"] == "succeeded"
    assert await balance(client, a) == 9900 and await balance(client, b) == 10100


async def test_lost_response_after_commit_retries_exactly_once(running):
    app, client = running
    a, b = (await fixture_users(client, 2))["users"]
    payload = body(a, b)

    async def fail(stage, conn):
        if stage == "after_commit":
            raise RuntimeError("simulated lost response after successful commit")

    app.state.service.failure_hook = fail
    response = await client.post("/transfers", json=payload, headers=auth(a["token"]))
    assert response.status_code == 500
    app.state.service.failure_hook = None
    response = await client.post("/transfers", json=payload, headers=auth(a["token"]))
    assert response.status_code == 200 and response.json()["status"] == "succeeded"
    assert await balance(client, a) == 9900 and await balance(client, b) == 10100
    events = (await client.get("/events")).json()["records"]
    assert len([e for e in events if e["transfer_id"] == response.json()["id"]]) == 3


async def test_same_key_different_bodies_racing_has_one_winner(running):
    _, client = running
    a, b = (await fixture_users(client, 2))["users"]
    key = str(uuid4())
    responses = await asyncio.gather(
        *[
            client.post("/transfers", json=body(a, b, amount, key), headers=auth(a["token"]))
            for amount in range(100, 120)
        ]
    )
    assert sum(r.status_code == 200 for r in responses) == 1
    assert sum(r.status_code == 409 for r in responses) == 19
    winner = next(r.json() for r in responses if r.status_code == 200)
    assert await balance(client, a) == 10000 - winner["amount_paise"]


async def test_auth_ownership_and_participant_visibility(running):
    _, client = running
    a, b, c = (await fixture_users(client, 3))["users"]
    assert (await client.post("/wallets")).status_code == 401
    assert (await client.post("/wallets", headers=auth("invalid"))).status_code == 401
    assert (await client.get("/wallets/" + a["wallet_id"], headers=auth(b["token"]))).status_code == 404
    payload = body(a, b)
    assert (await client.post("/transfers", json=payload, headers=auth(b["token"]))).status_code == 403
    result = await client.post("/transfers", json=payload, headers=auth(a["token"]))
    transfer_id = result.json()["id"]
    assert (await client.get("/transfers/" + transfer_id, headers=auth(b["token"]))).status_code == 200
    assert (await client.get("/transfers/" + transfer_id, headers=auth(c["token"]))).status_code == 404
    assert (await client.post("/admin/fixtures", json={}, headers=auth(a["token"]))).status_code == 403


async def test_same_key_is_scoped_to_authenticated_user(running):
    _, client = running
    a, b = (await fixture_users(client, 2))["users"]
    key = str(uuid4())
    results = await asyncio.gather(
        client.post("/transfers", json=body(a, b, key=key), headers=auth(a["token"])),
        client.post("/transfers", json=body(b, a, key=key), headers=auth(b["token"])),
    )
    assert all(r.status_code == 200 and r.json()["status"] == "succeeded" for r in results)
    assert results[0].json()["id"] != results[1].json()["id"]


async def test_destination_overflow_declines_without_debit(running):
    _, client = running
    a, b = (await fixture_users(client, 2, MAX_PAISE))["users"]
    result = await client.post("/transfers", json=body(a, b, 1), headers=auth(a["token"]))
    assert result.status_code == 200 and result.json()["reason"] == "destination_limit"
    assert await balance(client, a) == await balance(client, b) == MAX_PAISE


async def test_database_constraints_block_negative_balance_and_orphan_key(running):
    app, client = running
    a, b = (await fixture_users(client, 2))["users"]
    async with app.state.pool.connection() as conn:
        with pytest.raises(errors.CheckViolation):
            async with conn.transaction():
                await conn.execute("UPDATE wallets SET balance_paise=-1 WHERE id=%s", (UUID(a["wallet_id"]),))
        with pytest.raises(errors.ForeignKeyViolation):
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO idempotency_keys(user_id,key,request_body,transfer_id)
                    VALUES (%s,%s,'{}',%s)""",
                    (UUID(a["user_id"]), str(uuid4()), uuid4()),
                )


async def test_logs_metrics_and_correlation_do_not_expose_credentials(running):
    _, client = running
    metrics_before = (await client.get("/metrics")).text
    a, b = (await fixture_users(client, 2, 100))["users"]
    payload = body(a, b, 101)
    headers = {**auth(a["token"]), "X-Correlation-ID": "assessment-trace-123"}
    created = await client.post("/transfers", json=payload, headers=headers)
    assert created.headers["x-correlation-id"] == "assessment-trace-123"
    await client.post("/transfers", json=payload, headers=auth(a["token"]))
    logs = await client.get("/logs?limit=1000")
    metrics = await client.get("/metrics")
    assert "assessment-trace-123" in logs.text and "idempotent_replay_hit" in logs.text
    assert a["token"] not in logs.text and ADMIN_TOKEN not in logs.text

    def value(text, name):
        return float(next(line.split(" ")[-1] for line in text.splitlines() if line.startswith(name + " ")))

    for name in (
        'wallet_transfers_declined_total{reason="insufficient_funds"}',
        "wallet_idempotent_replays_total",
    ):
        assert value(metrics.text, name) - value(metrics_before, name) == 1
    assert "wallet_http_request_duration_seconds_bucket" in metrics.text


async def test_missing_destination_rolls_back_idempotency_reservation(running):
    app, client = running
    a, b = (await fixture_users(client, 2))["users"]
    payload = {**body(a, b), "to": str(uuid4())}
    result = await client.post("/transfers", json=payload, headers=auth(a["token"]))
    assert result.status_code == 404
    corrected = await client.post(
        "/transfers", json={**payload, "to": b["wallet_id"]}, headers=auth(a["token"])
    )
    assert corrected.status_code == 200
