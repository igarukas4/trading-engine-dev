-- Issue 59: durable Execution Coordination restart checkpoint.
-- The normalized execution tables in 006 remain the domain schema. This
-- checkpoint preserves the coordinator's complete account-scoped working set
-- across an API restart while the outbox and connector journal converge.
CREATE TABLE IF NOT EXISTS execution_state_snapshots (
    snapshot_id SMALLINT PRIMARY KEY CHECK (snapshot_id = 1),
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE order_intents
    ADD COLUMN IF NOT EXISTS requested_volume NUMERIC,
    ADD COLUMN IF NOT EXISTS cumulative_filled_volume NUMERIC NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS remaining_volume NUMERIC,
    ADD COLUMN IF NOT EXISTS risk_assessment_id UUID,
    ADD COLUMN IF NOT EXISTS command_id UUID;

ALTER TABLE order_intents
    DROP CONSTRAINT IF EXISTS order_intents_status_check;

ALTER TABLE order_intents
    ADD CONSTRAINT order_intents_status_check CHECK (
        status IN (
            'INTENT', 'CHECKED', 'DISPATCHING', 'SUBMITTED',
            'PARTIALLY_FILLED', 'REJECTED', 'UNKNOWN', 'FILLED', 'CANCELLED'
        )
    );

INSERT INTO schema_migrations (version)
VALUES ('010_execution_coordination_checkpoint')
ON CONFLICT (version) DO NOTHING;
