CREATE TABLE users (
    id UUID PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE wallets (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL UNIQUE REFERENCES users(id),
    balance_paise BIGINT NOT NULL DEFAULT 0
        CHECK (balance_paise BETWEEN 0 AND 9007199254740991),
    opening_balance_paise BIGINT NOT NULL DEFAULT 0
        CHECK (opening_balance_paise BETWEEN 0 AND 9007199254740991),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE transfers (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    idempotency_key VARCHAR(128) NOT NULL,
    from_wallet UUID NOT NULL REFERENCES wallets(id),
    to_wallet UUID NOT NULL REFERENCES wallets(id),
    amount_paise BIGINT NOT NULL CHECK (amount_paise BETWEEN 1 AND 9007199254740991),
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'declined')),
    reason TEXT CHECK (reason IN ('insufficient_funds', 'destination_limit')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, idempotency_key),
    CHECK (from_wallet <> to_wallet),
    CHECK ((status = 'succeeded' AND reason IS NULL) OR
           (status = 'declined' AND reason IS NOT NULL))
);

-- Reservation is separate so inserting it cannot take FK locks on wallet rows
-- before the application acquires those rows in sorted order.
CREATE TABLE idempotency_keys (
    user_id UUID NOT NULL REFERENCES users(id),
    key VARCHAR(128) NOT NULL,
    request_body JSONB NOT NULL,
    transfer_id UUID NOT NULL,
    PRIMARY KEY (user_id, key),
    CONSTRAINT idempotency_result_fk FOREIGN KEY (transfer_id) REFERENCES transfers(id)
        DEFERRABLE INITIALLY DEFERRED
);

-- Durable business events commit with money, including debit and credit.
-- Operational request/replay logs live on stdout and in a bounded memory tail.
CREATE TABLE domain_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transfer_id UUID NOT NULL REFERENCES transfers(id),
    event TEXT NOT NULL CHECK (event IN
        ('transfer_created', 'debited', 'credited', 'declined')),
    correlation_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (transfer_id, event)
);
