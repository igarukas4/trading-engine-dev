-- Issue 62: forward-only restart recovery and account-local convergence state.
-- A restored account stays fenced until a complete broker snapshot is committed.
ALTER TABLE execution_accounts
    ADD COLUMN IF NOT EXISTS recovery_status TEXT NOT NULL DEFAULT 'READY'
        CHECK (recovery_status IN ('READY', 'RECOVERING')),
    ADD COLUMN IF NOT EXISTS recovery_required BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS recovery_gate_before TEXT NOT NULL DEFAULT 'OPEN'
        CHECK (recovery_gate_before IN ('OPEN', 'FENCE_PENDING', 'QUARANTINED', 'STOPPED')),
    ADD COLUMN IF NOT EXISTS recovery_started_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS recovery_completed_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS account_recovery_checkpoints (
    broker_account_id UUID PRIMARY KEY REFERENCES broker_accounts(id),
    status TEXT NOT NULL CHECK (status IN ('READY', 'RECOVERING')),
    recovery_required BOOLEAN NOT NULL,
    recovery_gate_before TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    broker_snapshot_observed_at TIMESTAMPTZ,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version)
VALUES ('012_operational_convergence')
ON CONFLICT (version) DO NOTHING;
