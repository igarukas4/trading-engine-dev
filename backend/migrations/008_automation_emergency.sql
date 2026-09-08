-- T10: per-account automation fences and durable global emergency membership.
ALTER TABLE broker_accounts
    ADD COLUMN IF NOT EXISTS execution_mode_revision BIGINT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS mode_changed_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS global_emergency_operations (
    id UUID PRIMARY KEY,
    requested_kind TEXT NOT NULL CHECK (requested_kind IN ('STOP_ONLY', 'CLOSE_ALL')),
    status TEXT NOT NULL DEFAULT 'INCOMPLETE' CHECK (status IN ('INCOMPLETE', 'COMPLETE')),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS global_emergency_targets (
    operation_id UUID NOT NULL REFERENCES global_emergency_operations(id),
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONVERGED', 'UNRESOLVED')),
    detail TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (operation_id, broker_account_id)
);

INSERT INTO schema_migrations (version) VALUES ('008_automation_emergency') ON CONFLICT (version) DO NOTHING;
