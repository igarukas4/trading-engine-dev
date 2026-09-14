import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import test from "node:test";

const run = (script) => execFileSync(process.execPath, ["tests/python.mjs", "-c", script], { encoding: "utf8" });

test("RSS news is provenance-preserving, structured, and escalates only bounded low-confidence conflicts", () => {
  const output = run(`
from datetime import datetime, timezone, timedelta
from backend.app.news import NewsAnalyzer, NewsFeedIngestor

xml = '''<rss><channel><item><guid>g-1</guid><title>ECB signals patient policy</title><link>https://news.google.com/a</link><pubDate>Mon, 05 Jan 2026 10:00:00 GMT</pubDate><description>ECB comments</description></item></channel></rss>'''
event = NewsFeedIngestor("GOOGLE_NEWS").ingest(xml, observed_at=datetime(2026, 1, 5, 10, 1, tzinfo=timezone.utc))[0]
assert event.provider == "GOOGLE_NEWS"
assert event.source_url == "https://news.google.com/a"
assert event.raw_payload["xml"] == xml
calls = []
analysis = NewsAnalyzer().analyze(
    event,
    extractor=lambda _: {"canonical_pair_codes": ["EURUSD"], "currencies": ["EUR"], "directional_bias": "bullish", "sentiment": "positive", "severity": "MEDIUM", "confidence": "0.40", "trade_impact": "WATCH", "reason": "weak", "expires_at": "2026-01-05T12:00:00+00:00"},
    analyzer=lambda _: calls.append("analyzer") or {"canonical_pair_codes": ["EURUSD"], "currencies": ["EUR"], "directional_bias": "bullish", "sentiment": "positive", "severity": "MEDIUM", "confidence": "0.91", "trade_impact": "WATCH", "reason": "confirmed", "expires_at": "2026-01-05T12:00:00+00:00"},
)
assert calls == ["analyzer"]
assert analysis.confidence == 0.91
assert analysis.escalated is True
print("ok")
`);
  assert.match(output, /ok/);
});

test("account application resolves each account's own Pair and fails closed by policy", () => {
  const output = run(`
from datetime import datetime, timezone, timedelta
from backend.app.news import NewsAnalysis, NewsContextStore, NewsEvent

event = NewsEvent(id="event-1", provider="INVESTING", external_id="x", source_url="https://investing.com/x", headline="Oil", published_at=datetime(2026, 1, 5, tzinfo=timezone.utc), observed_at=datetime(2026, 1, 5, tzinfo=timezone.utc), raw_payload={})
analysis = NewsAnalysis(id="analysis-1", news_event_id=event.id, canonical_pair_codes=("EURUSD",), currencies=("EUR",), directional_bias="bearish", sentiment="negative", severity="HIGH", confidence=0.9, trade_impact="BLOCK", reason="event", expires_at=datetime(2026, 1, 5, 12, tzinfo=timezone.utc), model="test", prompt_version="p1", schema_version="v1", created_at=datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc))
store = NewsContextStore()
store.add_event(event); store.add_analysis(analysis)
a = store.apply(analysis.id, account_id="account-a", pair_mappings={"EURUSD": ("pair-a-eurusd", 3)}, policy={"news": "REQUIRED", "news_freshness_seconds": 3600}, now=datetime(2026, 1, 5, 10, tzinfo=timezone.utc))
b = store.apply(analysis.id, account_id="account-b", pair_mappings={"EURUSD": ("pair-b-eurusd", 8)}, policy={"news": "ADVISORY", "news_freshness_seconds": 3600}, now=datetime(2026, 1, 5, 10, tzinfo=timezone.utc))
assert a.pair_id == "pair-a-eurusd" and b.pair_id == "pair-b-eurusd"
assert a.account_id != b.account_id and a.mapping_version != b.mapping_version
try:
    store.apply(analysis.id, account_id="account-c", pair_mappings={}, policy={"news": "REQUIRED"}, now=datetime(2026, 1, 5, 10, tzinfo=timezone.utc))
except ValueError as error:
    assert str(error) == "REQUIRED_NEWS_UNAVAILABLE"
else:
    raise AssertionError("required news unexpectedly passed")
advisory = store.apply("analysis-missing", account_id="account-c", pair_mappings={}, policy={"news": "ADVISORY"}, now=datetime(2026, 1, 5, tzinfo=timezone.utc))
assert advisory.status == "ADVISORY_UNAVAILABLE" and advisory.audit_reason == "NEWS_ANALYSIS_MISSING"
print("ok")
`);
  assert.match(output, /ok/);
});

test("context revision creates a new Signal revision and invalidates approval", () => {
  const output = run(`
from datetime import datetime, timezone, timedelta
from backend.app.news import NewsContextStore
from backend.app.risk_calendar import RiskLimits
from backend.app.signals import SignalStore

signals = SignalStore()
at = datetime(2030, 9, 8, tzinfo=timezone.utc)
signal = signals.create(account_id="account-a", opportunity={"id": "opp", "account_id": "account-a", "pair": "EURUSD", "strategy_config_version_id": "config-account-a-v1"}, market_snapshot_id="snap", policy_version=1, created_at=at, ttl=timedelta(days=1), limits=RiskLimits(broker_account_id="account-a"), risk_kwargs={"baseline_samples": 20, "policy_healthy": True}, context_revision=1)
signal = signals.approve(signal.id, account_id="account-a", revision=1)
revised = NewsContextStore.revise_signal(signals, signal.id, account_id="account-a", context_revision=2, policy_version=1, limits=RiskLimits(broker_account_id="account-a"))
assert signals.get(signal.id).status == "INVALIDATED"
assert revised.revision == 2 and revised.context_revision == 2 and revised.supersedes_signal_id == signal.id
print("ok")
`);
  assert.match(output, /ok/);
});
