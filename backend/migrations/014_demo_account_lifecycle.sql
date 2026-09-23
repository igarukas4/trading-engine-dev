-- Durable account lifecycle command, readiness, and audit projections.
CREATE TABLE IF NOT EXISTS lifecycle_commands (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    action TEXT NOT NULL CHECK (action IN ('enable', 'start', 'stop', 'disable')),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    expected_version BIGINT NOT NULL,
    observed_version BIGINT NOT NULL,
    execution_epoch BIGINT NOT NULL,
    status TEXT NOT NULL,
    audit_id UUID NOT NULL,
    actor TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT 'lifecycle',
    reason TEXT NOT NULL,
    readiness_reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    prior_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    new_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (broker_account_id, actor, operation, idempotency_key)
);

CREATE TABLE IF NOT EXISTS lifecycle_readiness (
    broker_account_id UUID PRIMARY KEY REFERENCES broker_accounts(id),
    risk_limits_version BIGINT,
    pair_mappings JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lifecycle_audit (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    event_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE broker_accounts
    ADD COLUMN IF NOT EXISTS reconciliation_watermark TEXT,
    ADD COLUMN IF NOT EXISTS reconciliation_observed_at TIMESTAMPTZ;

ALTER TABLE lifecycle_commands
    ADD COLUMN IF NOT EXISTS operation TEXT NOT NULL DEFAULT 'lifecycle',
    ADD COLUMN IF NOT EXISTS response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_commands_scope_key
    ON lifecycle_commands (broker_account_id, actor, operation, idempotency_key);

INSERT INTO schema_migrations (version) VALUES ('014_demo_account_lifecycle') ON CONFLICT (version) DO NOTHING;
