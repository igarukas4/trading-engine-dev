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
    remaining_volume NUMERIC NOT NULL,
    accounting_mode TEXT NOT NULL DEFAULT 'NETTING'
        CHECK (accounting_mode IN ('NETTING', 'HEDGING')),
    external_position_id TEXT,
    lifecycle_stage TEXT NOT NULL DEFAULT 'ENTRY'
        CHECK (lifecycle_stage IN ('ENTRY', 'TP1_CONFIRMED', 'TP2_CONFIRMED', 'CLOSING', 'CLOSED')),
    protection_status TEXT NOT NULL CHECK (protection_status IN ('CONFIRMED', 'UNCONFIRMED', 'QUARANTINED')),
    native_stop_loss NUMERIC,
    native_take_profit NUMERIC,
    last_confirmed_stop NUMERIC,
    runner_volume NUMERIC,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, id),
    UNIQUE (broker_account_id, external_position_id)
);

-- Every partial exit or protection change is an auditable, idempotent command.
-- A PositionCommand is reduce-only by construction; broker confirmation, not
-- target crossing, advances the Position lifecycle stage.
CREATE TABLE IF NOT EXISTS position_commands (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    position_id UUID NOT NULL REFERENCES positions(id),
    command_type TEXT NOT NULL CHECK (command_type IN ('TP1', 'TP2', 'TRAIL', 'CLOSE')),
    requested_volume NUMERIC,
    reduce_only BOOLEAN NOT NULL DEFAULT TRUE CHECK (reduce_only = TRUE),
    requested_stop NUMERIC,
    confirmed_stop NUMERIC,
    status TEXT NOT NULL CHECK (status IN ('RECEIVED', 'CONFIRMED', 'UNKNOWN', 'REJECTED')),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    execution_epoch BIGINT NOT NULL,
    expected_volume NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, idempotency_key)
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

-- Operator intent is separate from broker execution and remains auditable.
CREATE TABLE IF NOT EXISTS operator_commands (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    signal_id UUID,
    kind TEXT NOT NULL CHECK (kind IN ('APPROVE_SIGNAL', 'EXECUTE_SIGNAL', 'CLOSE_ALL')),
    idempotency_key TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    confirmed BOOLEAN NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ACCEPTED', 'REJECTED', 'EXECUTED')),
    rejection_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, idempotency_key)
);

INSERT INTO schema_migrations (version) VALUES ('006_execution_safety') ON CONFLICT (version) DO NOTHING;
