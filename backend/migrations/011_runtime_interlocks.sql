-- Issue 61: account-local runtime interlocks, protection repair, and alerts.
-- These records gate new exposure without changing BrokerAccount Mode, LIVE
-- unlock, or BotState.
CREATE TABLE IF NOT EXISTS runtime_interlocks (
    broker_account_id UUID PRIMARY KEY REFERENCES broker_accounts(id),
    status TEXT NOT NULL CHECK (status IN ('ELIGIBLE', 'BLOCKED', 'QUARANTINED')),
    reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    recovery_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    requires_custodian_command BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runtime_interlock_alerts (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    reason_code TEXT NOT NULL,
    detail TEXT NOT NULL,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS protection_repairs (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    position_id UUID NOT NULL,
    requested_stop_loss NUMERIC NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    status TEXT NOT NULL CHECK (status IN ('RECOVERED', 'QUARANTINED')),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, position_id)
);

CREATE TABLE IF NOT EXISTS calendar_blackouts (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    pair TEXT,
    currencies JSONB NOT NULL DEFAULT '[]'::jsonb,
    scope_known BOOLEAN NOT NULL,
    reason_code TEXT NOT NULL,
    blackout_start TIMESTAMPTZ,
    blackout_end TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS manual_economic_event_overrides (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    pair TEXT,
    currencies JSONB NOT NULL DEFAULT '[]'::jsonb,
    blackout_start TIMESTAMPTZ NOT NULL,
    blackout_end TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version)
VALUES ('011_runtime_interlocks')
ON CONFLICT (version) DO NOTHING;
