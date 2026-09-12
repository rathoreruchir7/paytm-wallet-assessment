import hashlib
import secrets
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.config import MAX_PAISE
from app.database import transaction_limits
from app.observability import correlation_id


class DomainError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def wallet_body(row):
    return {"id": str(row["id"]), "balance_paise": row["balance_paise"]}


def transfer_body(row):
    # Never include current wallet balances: those would change on replay.
    return {
        "id": str(row["id"]),
        "from": str(row["from_wallet"]),
        "to": str(row["to_wallet"]),
        "amount_paise": row["amount_paise"],
        "idempotency_key": row["idempotency_key"],
        "status": row["status"],
        "reason": row["reason"],
        "created_at": row["created_at"].isoformat(),
    }


class WalletService:
    def __init__(self, pool, settings, obs, failure_hook=None):
        self.pool, self.settings, self.obs = pool, settings, obs
        # Constructor-only test injection; no HTTP header or env backdoor.
        self.failure_hook = failure_hook

    async def authenticate(self, token):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT id FROM users WHERE token_hash = %s", (token_hash(token),))
            ).fetchone()
        if not row:
            raise DomainError(401, "unauthorized", "Valid bearer token required")
        return row["id"]

    async def get_or_create(self, user_id):
        # Common replay path needs no write or row lock. Uniqueness still resolves
        # the creation race when multiple callers observe no wallet here.
        async with self.pool.connection() as conn:
            existing = await (
                await conn.execute("SELECT * FROM wallets WHERE user_id = %s", (user_id,))
            ).fetchone()
        if existing:
            self.obs.emit("wallet_reused", wallet_id=str(existing["id"]))
            return wallet_body(existing)
        async with self.pool.connection() as conn, conn.transaction():
            await transaction_limits(conn, self.settings)
            row = await (
                await conn.execute(
                    """INSERT INTO wallets (id, user_id)
                VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING RETURNING *""",
                    (uuid4(), user_id),
                )
            ).fetchone()
            created = row is not None
            if not row:
                # Separate statement: fresh READ COMMITTED snapshot after conflict wait.
                row = await (
                    await conn.execute("SELECT * FROM wallets WHERE user_id = %s", (user_id,))
                ).fetchone()
        self.obs.emit("wallet_created" if created else "wallet_reused", wallet_id=str(row["id"]))
        return wallet_body(row)

    async def get_wallet(self, user_id, wallet_id):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT * FROM wallets WHERE id = %s AND user_id = %s", (wallet_id, user_id)
                )
            ).fetchone()
        if not row:
            raise DomainError(404, "wallet_not_found", "Wallet not found")
        return wallet_body(row)

    async def get_transfer(self, user_id, transfer_id):
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    """SELECT t.* FROM transfers t
                JOIN wallets w ON w.id = t.to_wallet
                WHERE t.id = %s AND (t.user_id = %s OR w.user_id = %s)""",
                    (transfer_id, user_id, user_id),
                )
            ).fetchone()
        if not row:
            raise DomainError(404, "transfer_not_found", "Transfer not found")
        return transfer_body(row)

    async def transfer(self, user_id, body):
        canonical = {
            "from": str(body.from_wallet),
            "to": str(body.to_wallet),
            "amount_paise": body.amount_paise,
        }
        transfer_id, replay = uuid4(), False
        events = []
        async with self.pool.connection() as conn, conn.transaction():
            await transaction_limits(conn, self.settings)
            reservation = await (
                await conn.execute(
                    """INSERT INTO idempotency_keys
                (user_id, key, request_body, transfer_id) VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, key) DO NOTHING RETURNING transfer_id""",
                    (user_id, body.idempotency_key, Jsonb(canonical), transfer_id),
                )
            ).fetchone()
            if not reservation:
                original = await (
                    await conn.execute(
                        """SELECT request_body, transfer_id
                    FROM idempotency_keys WHERE user_id = %s AND key = %s""",
                        (user_id, body.idempotency_key),
                    )
                ).fetchone()
                if original["request_body"] != canonical:
                    self.obs.conflicts.inc()
                    self.obs.emit("idempotency_conflict")
                    raise DomainError(409, "idempotency_conflict", "Key already used with a different body")
                row = await (
                    await conn.execute("SELECT * FROM transfers WHERE id = %s", (original["transfer_id"],))
                ).fetchone()
                replay = True
            else:
                locked = {}
                # Two explicit single-row statements make lock acquisition order unambiguous.
                # Crucially, no transfer FKs are inserted before these locks.
                for wallet_id in sorted((body.from_wallet, body.to_wallet), key=lambda u: u.int):
                    wallet = await (
                        await conn.execute("SELECT * FROM wallets WHERE id = %s FOR UPDATE", (wallet_id,))
                    ).fetchone()
                    if not wallet:
                        raise DomainError(404, "wallet_not_found", "Wallet not found")
                    locked[wallet_id] = wallet
                source, destination = locked[body.from_wallet], locked[body.to_wallet]
                if source["user_id"] != user_id:
                    raise DomainError(403, "forbidden", "Source wallet must belong to the caller")
                reason = None
                if source["balance_paise"] < body.amount_paise:
                    reason = "insufficient_funds"
                elif destination["balance_paise"] > MAX_PAISE - body.amount_paise:
                    reason = "destination_limit"
                if reason is None:
                    debit = await conn.execute(
                        """UPDATE wallets SET balance_paise = balance_paise - %s
                        WHERE id = %s AND balance_paise >= %s""",
                        (body.amount_paise, body.from_wallet, body.amount_paise),
                    )
                    if debit.rowcount != 1:
                        raise RuntimeError("Locked debit invariant violated")
                    if self.failure_hook:
                        await self.failure_hook("after_debit", conn)
                events = (
                    ["transfer_created", "declined"]
                    if reason
                    else ["transfer_created", "debited", "credited"]
                )
                # These dependent statements execute in order in the same transaction.
                # Pipeline mode batches network traffic; it does not commit or parallelize SQL.
                async with conn.pipeline():
                    if reason is None:
                        await conn.execute(
                            "UPDATE wallets SET balance_paise = balance_paise + %s WHERE id = %s",
                            (body.amount_paise, body.to_wallet),
                        )
                    receipt = await conn.execute(
                        """INSERT INTO transfers
                    (id, user_id, idempotency_key, from_wallet, to_wallet, amount_paise, status, reason)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (
                            transfer_id,
                            user_id,
                            body.idempotency_key,
                            body.from_wallet,
                            body.to_wallet,
                            body.amount_paise,
                            "declined" if reason else "succeeded",
                            reason,
                        ),
                    )
                    for event in events:
                        await conn.execute(
                            """INSERT INTO domain_events (transfer_id, event, correlation_id)
                            VALUES (%s, %s, %s)""",
                            (transfer_id, event, correlation_id.get()),
                        )
                row = await receipt.fetchone()
                if self.failure_hook:
                    await self.failure_hook("before_commit", conn)
        # The transaction has committed before successful logs/counters or a response.
        if self.failure_hook and not replay:
            await self.failure_hook("after_commit", None)
        if replay:
            self.obs.replays.inc()
            self.obs.emit("idempotent_replay_hit", transfer_id=str(row["id"]))
        else:
            self.obs.created.inc()
            if row["reason"]:
                self.obs.declined.labels(row["reason"]).inc()
            for event in events:
                self.obs.emit(event, transfer_id=str(row["id"]), reason=row["reason"])
        return transfer_body(row)

    async def fixtures(self, count, amount):
        users = []
        async with self.pool.connection() as conn, conn.transaction():
            await transaction_limits(conn, self.settings)
            for index in range(count + 1):
                uid, token = uuid4(), secrets.token_urlsafe(32)
                await conn.execute(
                    "INSERT INTO users (id, token_hash) VALUES (%s, %s)", (uid, token_hash(token))
                )
                user = {"user_id": str(uid), "token": token}
                if index < count:
                    wid = uuid4()
                    await conn.execute(
                        """INSERT INTO wallets
                        (id,user_id,balance_paise,opening_balance_paise) VALUES (%s,%s,%s,%s)""",
                        (wid, uid, amount, amount),
                    )
                    user.update(wallet_id=str(wid), balance_paise=amount)
                    users.append(user)
                else:
                    fresh_user = user
        self.obs.emit("fixtures_created", funded_wallets=count)
        return {
            "users": users,
            "fresh_user": fresh_user,
            "note": "Synthetic test money; fresh_user has no wallet. Tokens are returned only here.",
        }

    async def events(self, limit):
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """SELECT e.id, e.transfer_id, e.event,
                e.correlation_id, e.created_at FROM domain_events e ORDER BY e.id DESC LIMIT %s""",
                    (limit,),
                )
            ).fetchall()
        return [
            {**r, "transfer_id": str(r["transfer_id"]), "created_at": r["created_at"].isoformat()}
            for r in rows
        ]
