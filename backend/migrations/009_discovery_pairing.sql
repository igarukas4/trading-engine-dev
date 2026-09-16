-- T11: expiring Account Discovery Pairing state. Secrets are never stored.
CREATE TABLE IF NOT EXISTS pairing_sessions (
    id UUID PRIMARY KEY,
    custodian_session_id TEXT NOT NULL,
    code_salt TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'CANDIDATE_SUBMITTED', 'CONFIRMED', 'CANCELLED', 'EXPIRED')),
    candidate JSONB,
    connector_session_id TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    account_id UUID REFERENCES broker_accounts(id),
    key_id TEXT,
    key_delivered BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pairing_sessions_active_idx
    ON pairing_sessions (status, expires_at);

INSERT INTO schema_migrations (version)
VALUES ('009_discovery_pairing')
ON CONFLICT (version) DO NOTHING;
