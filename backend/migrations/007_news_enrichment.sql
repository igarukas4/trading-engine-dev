-- T9: shared news provenance/analysis with account-owned context projections.
CREATE TABLE IF NOT EXISTS news_events (
    id UUID PRIMARY KEY,
    provider TEXT NOT NULL CHECK (provider IN ('GOOGLE_NEWS', 'INVESTING')),
    external_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    headline TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    content_hash TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    UNIQUE (provider, external_id)
);

CREATE TABLE IF NOT EXISTS news_analyses (
    id UUID PRIMARY KEY,
    news_event_id UUID NOT NULL REFERENCES news_events(id),
    canonical_pair_codes TEXT[] NOT NULL DEFAULT '{}',
    currencies TEXT[] NOT NULL DEFAULT '{}',
    directional_bias TEXT NOT NULL CHECK (directional_bias IN ('bullish', 'bearish', 'neutral')),
    sentiment TEXT NOT NULL,
    severity TEXT NOT NULL,
    confidence NUMERIC NOT NULL,
    trade_impact TEXT NOT NULL,
    reason TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    escalated BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS market_context_projections (
    id UUID PRIMARY KEY,
    broker_account_id UUID NOT NULL REFERENCES broker_accounts(id),
    pair_id UUID REFERENCES pairs(id),
    news_analysis_id UUID REFERENCES news_analyses(id),
    mapping_version INTEGER,
    enrichment_policy_version INTEGER,
    context_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    audit_reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (broker_account_id, context_revision)
);

ALTER TABLE signals ADD COLUMN IF NOT EXISTS context_revision INTEGER NOT NULL DEFAULT 0;
INSERT INTO schema_migrations (version) VALUES ('007_news_enrichment') ON CONFLICT (version) DO NOTHING;
