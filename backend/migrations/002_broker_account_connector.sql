-- T1: immutable account identity and account-bound connector foundation.
CREATE TABLE IF NOT EXISTS broker_accounts (
    id UUID PRIMARY KEY,
    provider TEXT NOT NULL,
    broker_server TEXT NOT NULL,
    external_account_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('DEMO', 'LIVE')),
    lifecycle_status TEXT NOT NULL DEFAULT 'DISABLED' CHECK (lifecycle_status IN ('DISABLED', 'ENABLED', 'ARCHIVED')),
    bot_state TEXT NOT NULL DEFAULT 'STOPPED' CHECK (bot_state IN ('STOPPED', 'RUNNING', 'EMERGENCY_STOP')),
    execution_mode TEXT NOT NULL DEFAULT 'MANUAL' CHECK (execution_mode IN ('MANUAL', 'SEMI_AUTO', 'FULL_AUTO')),
    live_execution_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    execution_epoch BIGINT NOT NULL DEFAULT 0,
    connector_generation BIGINT NOT NULL DEFAULT 0,
    pending_generation BIGINT,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    connector_status TEXT NOT NULL DEFAULT 'UNAVAILABLE',
    reconciliation_status TEXT NOT NULL DEFAULT 'INCOMPLETE',
    reconciliation_watermark TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    version BIGINT NOT NULL DEFAULT 1,
    UNIQUE (provider, broker_server, external_account_id)
);

CREATE TABLE IF NOT EXISTS connector_bindings (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    provider TEXT NOT NULL,
    broker_server TEXT NOT NULL,
    external_account_id TEXT NOT NULL,
    key_id TEXT NOT NULL UNIQUE,
    secret_salt TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    UNIQUE (broker_account_id),
    FOREIGN KEY (provider, broker_server, external_account_id) REFERENCES broker_accounts(provider, broker_server, external_account_id)
);

CREATE TABLE IF NOT EXISTS broker_account_snapshots (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    observed_at TIMESTAMPTZ NOT NULL,
    source_generation BIGINT NOT NULL,
    payload JSONB NOT NULL,
    UNIQUE (broker_account_id, observed_at, source_generation)
);

CREATE TABLE IF NOT EXISTS broker_account_audit_events (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    event_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES ('002_broker_account_connector') ON CONFLICT (version) DO NOTHING;
