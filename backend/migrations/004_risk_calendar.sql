-- T4: immutable account-scoped risk and calendar policy foundations.
CREATE TABLE IF NOT EXISTS risk_limits (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    version INTEGER NOT NULL,
    max_risk_per_trade NUMERIC NOT NULL,
    daily_loss_limit NUMERIC NOT NULL,
    max_open_positions INTEGER NOT NULL,
    max_total_open_risk NUMERIC NOT NULL,
    max_currency_exposure NUMERIC NOT NULL,
    max_spread_multiple NUMERIC NOT NULL,
    max_slippage_r NUMERIC NOT NULL,
    max_volatility_atr_multiple NUMERIC NOT NULL,
    baseline_window_sessions INTEGER NOT NULL,
    baseline_minimum_samples INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, version)
);

CREATE TABLE IF NOT EXISTS enrichment_policies (
    id UUID PRIMARY KEY,
    strategy_config_id UUID NOT NULL,
    version INTEGER NOT NULL,
    source_rules JSONB NOT NULL,
    freshness_ttls JSONB NOT NULL,
    required_currencies TEXT[] NOT NULL DEFAULT '{}',
    blackout_before_minutes INTEGER NOT NULL,
    blackout_after_minutes INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy_config_id, version)
);

CREATE TABLE IF NOT EXISTS calendar_health (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    currency TEXT NOT NULL,
    provider TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    healthy BOOLEAN NOT NULL,
    covered BOOLEAN NOT NULL,
    source_revision TEXT NOT NULL,
    UNIQUE (broker_account_id, currency, source_revision)
);

CREATE TABLE IF NOT EXISTS safety_fences (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    sequence BIGINT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('FENCE_PENDING', 'ACKNOWLEDGED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_account_id, sequence)
);

INSERT INTO schema_migrations (version) VALUES ('004_risk_calendar') ON CONFLICT (version) DO NOTHING;
