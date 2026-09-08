-- Foundation schema only. Trading and broker effects are intentionally absent.
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS application_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO application_metadata (key, value)
VALUES ('foundation_version', '0.1.0')
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now();

INSERT INTO schema_migrations (version)
VALUES ('001_foundation')
ON CONFLICT (version) DO NOTHING;
