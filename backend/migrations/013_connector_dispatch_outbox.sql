-- Issue 66: durable account-scoped connector dispatch metadata.
-- The coordinator checkpoint remains the local test store; PostgreSQL deployments
-- retain a normalized outbox row for operational inspection and migration safety.
CREATE TABLE IF NOT EXISTS connector_dispatch_outbox (
    command_id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    dispatch_sequence BIGINT NOT NULL,
    connector_generation BIGINT NOT NULL,
    execution_epoch BIGINT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    command_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('QUEUED', 'SENT', 'ACCEPTED', 'REJECTED', 'UNKNOWN')),
    result_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, dispatch_sequence),
    UNIQUE (broker_account_id, idempotency_key)
);

INSERT INTO schema_migrations (version)
VALUES ('013_connector_dispatch_outbox')
ON CONFLICT (version) DO NOTHING;
