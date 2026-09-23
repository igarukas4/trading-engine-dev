-- Issue 72: lifecycle transaction metadata and durable execution fencing.
ALTER TABLE broker_accounts
    ADD COLUMN IF NOT EXISTS reconciliation_watermark TEXT,
    ADD COLUMN IF NOT EXISTS reconciliation_observed_at TIMESTAMPTZ;

ALTER TABLE broker_accounts
    ALTER COLUMN execution_epoch SET DEFAULT 1;
UPDATE broker_accounts
   SET execution_epoch = 1
 WHERE execution_epoch = 0 AND lifecycle_status = 'DISABLED';

ALTER TABLE lifecycle_commands
    ADD COLUMN IF NOT EXISTS actor TEXT NOT NULL DEFAULT 'migration',
    ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT 'migration',
    ADD COLUMN IF NOT EXISTS readiness_reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS prior_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS new_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS operation TEXT NOT NULL DEFAULT 'lifecycle',
    ADD COLUMN IF NOT EXISTS response_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;

ALTER TABLE lifecycle_audit
    ADD COLUMN IF NOT EXISTS actor TEXT NOT NULL DEFAULT 'migration',
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ACCEPTED',
    ADD COLUMN IF NOT EXISTS payload JSONB NOT NULL DEFAULT '{}'::jsonb;

-- The original issue-72 table keyed idempotency only by account. Replace it
-- with the authenticated principal and operation scope.
ALTER TABLE lifecycle_commands
    DROP CONSTRAINT IF EXISTS lifecycle_commands_broker_account_id_idempotency_key_key;
ALTER TABLE lifecycle_commands
    DROP CONSTRAINT IF EXISTS lifecycle_commands_broker_account_id_actor_operation_idempotency_key_key;
DROP INDEX IF EXISTS lifecycle_commands_broker_account_id_idempotency_key_key;
CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_commands_scope_key
    ON lifecycle_commands (broker_account_id, actor, operation, idempotency_key);

INSERT INTO schema_migrations (version)
VALUES ('015_lifecycle_atomicity')
ON CONFLICT (version) DO NOTHING;
