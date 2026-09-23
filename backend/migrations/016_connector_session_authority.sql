-- Issue 72: distinguish a current authenticated WSS session from stale health flags.
ALTER TABLE broker_accounts
    ADD COLUMN IF NOT EXISTS connector_session_id TEXT,
    ADD COLUMN IF NOT EXISTS connector_session_generation BIGINT;

INSERT INTO schema_migrations (version)
VALUES ('016_connector_session_authority')
ON CONFLICT (version) DO NOTHING;
