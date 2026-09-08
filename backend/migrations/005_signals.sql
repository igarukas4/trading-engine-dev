-- T5: immutable account-scoped Opportunities, Signals, and RiskAssessments.
CREATE TABLE IF NOT EXISTS opportunities (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    strategy_config_version_id UUID NOT NULL,
    market_state_snapshot_id UUID NOT NULL REFERENCES market_state_snapshots(id),
    evaluation_key JSONB NOT NULL,
    pair_id UUID NOT NULL REFERENCES pairs(id),
    direction TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    confidence NUMERIC NOT NULL,
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, evaluation_key)
);

CREATE TABLE IF NOT EXISTS signals (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    opportunity_id UUID NOT NULL REFERENCES opportunities(id),
    strategy_config_version_id UUID NOT NULL,
    market_state_snapshot_id UUID NOT NULL REFERENCES market_state_snapshots(id),
    enrichment_policy_version INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    supersedes_signal_id UUID REFERENCES signals(id),
    direction TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    entry_zone JSONB NOT NULL,
    stop_loss NUMERIC NOT NULL,
    take_profit JSONB NOT NULL,
    status TEXT NOT NULL,
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    UNIQUE (broker_account_id, opportunity_id, revision)
);

CREATE TABLE IF NOT EXISTS risk_assessments (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    signal_id UUID NOT NULL REFERENCES signals(id),
    risk_limits_version INTEGER,
    purpose TEXT NOT NULL CHECK (purpose IN ('INITIAL', 'PRE_ORDER')),
    approved BOOLEAN NOT NULL,
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    assessed_at TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    UNIQUE (signal_id, purpose, assessed_at)
);

INSERT INTO schema_migrations (version) VALUES ('005_signals') ON CONFLICT (version) DO NOTHING;
