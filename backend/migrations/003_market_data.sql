-- T2: canonical account-scoped market data and immutable completeness records.
CREATE TABLE IF NOT EXISTS pairs (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    canonical_code TEXT NOT NULL,
    broker_symbol TEXT NOT NULL,
    UNIQUE (broker_account_id, canonical_code),
    UNIQUE (broker_account_id, broker_symbol)
);

CREATE TABLE IF NOT EXISTS candles (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    pair_id UUID NOT NULL REFERENCES pairs(id),
    timeframe TEXT NOT NULL,
    open_time TIMESTAMPTZ NOT NULL,
    close_time TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL, high NUMERIC NOT NULL, low NUMERIC NOT NULL, close NUMERIC NOT NULL,
    tick_volume BIGINT NOT NULL DEFAULT 0, real_volume BIGINT NOT NULL DEFAULT 0,
    source_revision TEXT NOT NULL, is_closed BOOLEAN NOT NULL,
    UNIQUE (broker_account_id, pair_id, timeframe, open_time, source_revision)
);

CREATE TABLE IF NOT EXISTS indicator_values (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    pair_id UUID NOT NULL REFERENCES pairs(id),
    timeframe TEXT NOT NULL, candle_open_time TIMESTAMPTZ NOT NULL,
    indicator_definition_id UUID NOT NULL, parameter_hash TEXT NOT NULL,
    value_name TEXT NOT NULL, value_numeric NUMERIC NOT NULL
);

CREATE TABLE IF NOT EXISTS market_state_snapshots (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    pair_id UUID NOT NULL REFERENCES pairs(id),
    trigger_timeframe TEXT NOT NULL, trigger_time TIMESTAMPTZ NOT NULL,
    candle_revision_ids UUID[] NOT NULL, indicator_value_ids UUID[] NOT NULL DEFAULT '{}',
    completeness TEXT NOT NULL CHECK (completeness IN ('COMPLETE', 'INCOMPLETE', 'GAP_DETECTED')),
    reason_codes TEXT[] NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_migrations (version) VALUES ('003_market_data') ON CONFLICT (version) DO NOTHING;
