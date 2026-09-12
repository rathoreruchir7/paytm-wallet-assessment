-- Read-only diagnostic. A single REPEATABLE READ snapshot avoids mixing states.
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

-- Must match: only fixture provisioning establishes opening balances.
SELECT sum(balance_paise) AS total_current_paise,
       sum(opening_balance_paise) AS total_opening_paise,
       min(balance_paise) AS minimum_paise
FROM wallets;

-- Must return zero rows: check every wallet against immutable transfer receipts.
WITH movements AS (
    SELECT from_wallet AS wallet_id, -amount_paise AS delta
    FROM transfers WHERE status = 'succeeded'
    UNION ALL
    SELECT to_wallet AS wallet_id, amount_paise AS delta
    FROM transfers WHERE status = 'succeeded'
)
SELECT w.id, w.balance_paise,
       w.opening_balance_paise + coalesce(sum(m.delta), 0) AS expected_paise
FROM wallets w LEFT JOIN movements m ON m.wallet_id = w.id
GROUP BY w.id
HAVING w.balance_paise <> w.opening_balance_paise + coalesce(sum(m.delta), 0);

-- Must return zero rows: every key resolves to the owner's original transfer.
SELECT i.user_id, i.key FROM idempotency_keys i
LEFT JOIN transfers t ON t.id = i.transfer_id
WHERE t.id IS NULL OR t.user_id <> i.user_id OR t.idempotency_key <> i.key;

COMMIT;
