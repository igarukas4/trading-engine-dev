-- T5: immutable account-scoped Opportunities, Signals, and RiskAssessments.
CREATE UNIQUE INDEX IF NOT EXISTS market_state_snapshots_account_id_key
    ON market_state_snapshots (broker_account_id, id);
ALTER TABLE enrichment_policies ADD COLUMN IF NOT EXISTS broker_account_id UUID
    REFERENCES broker_accounts(id);
CREATE UNIQUE INDEX IF NOT EXISTS enrichment_policies_account_version_key
    ON enrichment_policies (broker_account_id, strategy_config_id, version);

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
ALTER TABLE opportunities ADD CONSTRAINT opportunities_account_id_key
    UNIQUE (broker_account_id, id);

CREATE TABLE IF NOT EXISTS signals (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    opportunity_id UUID NOT NULL,
    strategy_config_version_id UUID NOT NULL,
    market_state_snapshot_id UUID NOT NULL,
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
CREATE UNIQUE INDEX IF NOT EXISTS signals_account_id_key
    ON signals (broker_account_id, id);
ALTER TABLE signals ADD CONSTRAINT signals_opportunity_account_fk
    FOREIGN KEY (broker_account_id, opportunity_id)
    REFERENCES opportunities (broker_account_id, id);
ALTER TABLE signals ADD CONSTRAINT signals_snapshot_account_fk
    FOREIGN KEY (broker_account_id, market_state_snapshot_id)
    REFERENCES market_state_snapshots (broker_account_id, id);

CREATE TABLE IF NOT EXISTS risk_assessments (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    signal_id UUID NOT NULL,
    risk_limits_version INTEGER,
    purpose TEXT NOT NULL CHECK (purpose IN ('INITIAL', 'PRE_ORDER')),
    approved BOOLEAN NOT NULL,
    reason_codes TEXT[] NOT NULL DEFAULT '{}',
    assessed_at TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    UNIQUE (signal_id, purpose, assessed_at)
);
ALTER TABLE risk_assessments ADD CONSTRAINT risk_assessments_signal_account_fk
    FOREIGN KEY (broker_account_id, signal_id)
    REFERENCES signals (broker_account_id, id);

INSERT INTO schema_migrations (version) VALUES ('005_signals') ON CONFLICT (version) DO NOTHING;
