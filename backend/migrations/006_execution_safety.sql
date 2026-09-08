-- T6: account-local pre-order, dispatch, broker journal, and recovery records.
-- Every broker side effect is durable before connector invocation.
CREATE TABLE IF NOT EXISTS execution_accounts (
    broker_account_id UUID PRIMARY KEY REFERENCES broker_accounts(id),
    execution_epoch BIGINT NOT NULL DEFAULT 1,
    next_dispatch_sequence BIGINT NOT NULL DEFAULT 1,
    exposure_gate TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (exposure_gate IN ('OPEN', 'FENCE_PENDING', 'QUARANTINED', 'STOPPED')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS risk_reservations (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    signal_id UUID NOT NULL,
    risk_amount NUMERIC NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CONSUMED', 'RELEASED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, id)
);

CREATE TABLE IF NOT EXISTS order_intents (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    signal_id UUID NOT NULL,
    risk_reservation_id UUID NOT NULL REFERENCES risk_reservations(id),
    idempotency_key TEXT NOT NULL,
    canonical_hash TEXT NOT NULL,
    execution_epoch BIGINT NOT NULL,
    dispatch_sequence BIGINT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('INTENT', 'DISPATCHING', 'SUBMITTED', 'REJECTED', 'UNKNOWN', 'FILLED', 'CANCELLED')),
    external_order_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, idempotency_key),
    UNIQUE (broker_account_id, dispatch_sequence)
);

CREATE TABLE IF NOT EXISTS outbox_events (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    order_intent_id UUID NOT NULL UNIQUE REFERENCES order_intents(id),
    dispatch_sequence BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'DISPATCHING', 'PUBLISHED', 'ABORTED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, dispatch_sequence)
);

CREATE TABLE IF NOT EXISTS connector_journal (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    order_intent_id UUID NOT NULL UNIQUE REFERENCES order_intents(id),
    dispatch_sequence BIGINT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('PREPARED', 'DISPATCHING', 'ACCEPTED', 'REJECTED', 'ABORTED_NOT_INVOKED')),
    external_order_id TEXT,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, dispatch_sequence)
);

CREATE TABLE IF NOT EXISTS fills (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    order_intent_id UUID NOT NULL REFERENCES order_intents(id),
    external_deal_id TEXT NOT NULL,
    volume NUMERIC NOT NULL,
    native_protection_confirmed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, external_deal_id)
);

CREATE TABLE IF NOT EXISTS positions (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    order_intent_id UUID NOT NULL REFERENCES order_intents(id),
    volume NUMERIC NOT NULL,
    protection_status TEXT NOT NULL CHECK (protection_status IN ('CONFIRMED', 'UNCONFIRMED', 'QUARANTINED')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, id)
);

CREATE TABLE IF NOT EXISTS execution_fences (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    sequence BIGINT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('FENCE_PENDING', 'ACKNOWLEDGED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, sequence)
);

INSERT INTO schema_migrations (version) VALUES ('006_execution_safety') ON CONFLICT (version) DO NOTHING;
