"""Account-neutral news ingestion and account-owned context projections."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Literal, Mapping
from uuid import uuid4
from xml.etree import ElementTree


NewsPolicy = Literal["REQUIRED", "ADVISORY", "DISABLED"]
NewsProvider = Literal["GOOGLE_NEWS", "INVESTING"]
DirectionalBias = Literal["bullish", "bearish", "neutral"]

_DIRECTIONAL_BIASES = {"bullish", "bearish", "neutral"}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class NewsEvent:
    id: str
    provider: NewsProvider
    external_id: str
    source_url: str
    headline: str
    published_at: datetime
    observed_at: datetime
    raw_payload: Mapping[str, Any]
    content_hash: str = ""

    def __post_init__(self) -> None:
        if self.provider not in {"GOOGLE_NEWS", "INVESTING"}:
            raise ValueError("UNSUPPORTED_NEWS_PROVIDER")
        if not self.content_hash:
            encoded = json.dumps(self.raw_payload, sort_keys=True, default=str).encode()
            object.__setattr__(self, "content_hash", hashlib.sha256(encoded).hexdigest())


class NewsFeedIngestor:
    """Parse RSS while retaining the complete source document as provenance."""

    def __init__(self, provider: NewsProvider, feed_url: str = "") -> None:
        self.provider = provider
        self.feed_url = feed_url

    def ingest(self, xml: str, *, observed_at: datetime) -> list[NewsEvent]:
        root = ElementTree.fromstring(xml)
        events: list[NewsEvent] = []
        for item in root.findall(".//item"):
            published = self._value(item, "pubDate")
            published_at = parsedate_to_datetime(published) if published else observed_at
            published_at = _utc(published_at)
            guid = self._value(item, "guid")
            link = self._value(item, "link")
            title = self._value(item, "title")
            external_id = guid or link or hashlib.sha256(title.encode()).hexdigest()
            item_payload = {
                "guid": external_id,
                "title": title,
                "link": link,
                "description": self._value(item, "description"),
                "pubDate": published,
            }
            events.append(
                NewsEvent(
                    id=f"news-event-{uuid4()}",
                    provider=self.provider,
                    external_id=external_id,
                    source_url=link or self.feed_url,
                    headline=title,
                    published_at=published_at,
                    observed_at=_utc(observed_at),
                    raw_payload={"xml": xml, "item": item_payload},
                )
            )
        return events

    @staticmethod
    def _value(item: ElementTree.Element, name: str) -> str:
        node = item.find(name)
        return (node.text or "").strip() if node is not None else ""


@dataclass(frozen=True)
class NewsAnalysis:
    id: str
    news_event_id: str
    canonical_pair_codes: tuple[str, ...]
    currencies: tuple[str, ...]
    directional_bias: DirectionalBias
    sentiment: str
    severity: str
    confidence: float
    trade_impact: str
    reason: str
    expires_at: datetime
    model: str
    prompt_version: str
    schema_version: str
    created_at: datetime
    escalated: bool = False


class NewsAnalyzer:
    """Structured extraction with one bounded, explicit escalation."""

    def __init__(self, *, confidence_threshold: float = 0.65) -> None:
        self.confidence_threshold = confidence_threshold

    def analyze(
        self,
        event: NewsEvent,
        *,
        extractor: Callable[[NewsEvent], dict[str, Any]],
        analyzer: Callable[[NewsEvent], dict[str, Any]] | None = None,
    ) -> NewsAnalysis:
        payload = extractor(event)
        confidence = float(payload.get("confidence", 0))
        conflict = bool(payload.get("conflict", False))
        escalated = False
        if (confidence < self.confidence_threshold or conflict) and analyzer is not None:
            payload = analyzer(event)
            escalated = True
        required = (
            "canonical_pair_codes",
            "currencies",
            "directional_bias",
            "sentiment",
            "severity",
            "trade_impact",
            "reason",
            "expires_at",
        )
        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError("NEWS_STRUCTURED_OUTPUT_INCOMPLETE")
        bias = payload["directional_bias"]
        if bias not in _DIRECTIONAL_BIASES:
            raise ValueError("NEWS_DIRECTIONAL_BIAS_INVALID")
        return NewsAnalysis(
            id=f"news-analysis-{uuid4()}", news_event_id=event.id,
            canonical_pair_codes=tuple(dict.fromkeys(payload["canonical_pair_codes"])),
            currencies=tuple(dict.fromkeys(payload["currencies"])), directional_bias=bias,
            sentiment=str(payload["sentiment"]), severity=str(payload["severity"]),
            confidence=float(payload.get("confidence", 0)), trade_impact=str(payload["trade_impact"]),
            reason=str(payload["reason"]), expires_at=_utc(datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))),
            model="analyzer" if escalated else "extractor", prompt_version="v1", schema_version="news-analysis-v1",
            created_at=event.observed_at, escalated=escalated,
        )


@dataclass(frozen=True)
class ContextProjection:
    id: str
    account_id: str
    pair_id: str | None
    news_analysis_id: str | None
    mapping_version: int | None
    policy_version: int | None
    context_revision: int
    status: str
    audit_reason: str
    created_at: datetime


class NewsContextStore:
    def __init__(self) -> None:
        self.events: dict[str, NewsEvent] = {}
        self.analyses: dict[str, NewsAnalysis] = {}
        self.projections: dict[str, ContextProjection] = {}
        self._revisions: dict[str, int] = {}

    def add_event(self, event: NewsEvent) -> NewsEvent:
        self.events[event.id] = event
        return event

    def add_analysis(self, analysis: NewsAnalysis) -> NewsAnalysis:
        if analysis.news_event_id not in self.events:
            raise ValueError("NEWS_EVENT_NOT_FOUND")
        self.analyses[analysis.id] = analysis
        return analysis

    @staticmethod
    def _policy(policy: Any) -> tuple[NewsPolicy, int, int]:
        if isinstance(policy, dict):
            mode = policy.get("news", "DISABLED")
            version = int(policy.get("version", 1))
            ttl = int(policy.get("news_freshness_seconds", 3600))
        else:
            mode = policy.source_rules.get("news", "DISABLED")
            version = policy.version
            ttl = policy.freshness_ttl_seconds.get("news", 3600)
        return mode, version, ttl

    def apply(
        self,
        analysis_id: str,
        *,
        account_id: str,
        pair_mappings: dict[str, Any],
        policy: Any,
        now: datetime,
    ) -> ContextProjection:
        mode, policy_version, ttl = self._policy(policy)
        revision = self._revisions.get(account_id, 0) + 1
        self._revisions[account_id] = revision
        analysis = self.analyses.get(analysis_id)
        status, reason = "APPLIED", ""
        pair_id = None
        mapping_version = None
        if mode == "DISABLED":
            status, reason = "DISABLED", "NEWS_DISABLED"
        elif analysis is None:
            status, reason = "ADVISORY_UNAVAILABLE", "NEWS_ANALYSIS_MISSING"
            if mode == "REQUIRED":
                raise ValueError("REQUIRED_NEWS_UNAVAILABLE")
        elif self._is_stale(analysis, now, ttl):
            status, reason = "ADVISORY_UNAVAILABLE", "NEWS_ANALYSIS_STALE"
            if mode == "REQUIRED":
                raise ValueError("REQUIRED_NEWS_STALE")
        else:
            pair_id, mapping_version = self._resolve_mapping(
                analysis, account_id, pair_mappings
            )
            if pair_id is None:
                status, reason = "ADVISORY_UNAVAILABLE", "NEWS_PAIR_MAPPING_MISSING"
                if mode == "REQUIRED":
                    raise ValueError("REQUIRED_NEWS_UNAVAILABLE")
        projection = ContextProjection(
            id=f"context-{uuid4()}",
            account_id=account_id,
            pair_id=pair_id,
            news_analysis_id=analysis_id if analysis else None,
            mapping_version=mapping_version,
            policy_version=policy_version,
            context_revision=revision,
            status=status,
            audit_reason=reason,
            created_at=_utc(now),
        )
        self.projections[projection.id] = projection
        return projection

    @staticmethod
    def _is_stale(analysis: NewsAnalysis, now: datetime, ttl: int) -> bool:
        current = _utc(now)
        return current >= analysis.expires_at or (current - analysis.created_at).total_seconds() > ttl

    @staticmethod
    def _resolve_mapping(
        analysis: NewsAnalysis,
        account_id: str,
        pair_mappings: dict[str, Any],
    ) -> tuple[str | None, int | None]:
        for code in analysis.canonical_pair_codes:
            mapping = pair_mappings.get(code)
            if not mapping:
                continue
            if hasattr(mapping, "account_id") and mapping.account_id != account_id:
                raise ValueError("ACCOUNT_CONTEXT_MISMATCH")
            if isinstance(mapping, tuple):
                return mapping
            pair_id = getattr(mapping, "pair_id", getattr(mapping, "id", None))
            version = getattr(mapping, "mapping_version", getattr(mapping, "version", 1))
            return pair_id, version
        return None, None

    @staticmethod
    def revise_signal(
        signal_store: Any,
        signal_id: str,
        *,
        account_id: str,
        context_revision: int,
        policy_version: int,
        limits: Any,
    ) -> Any:
        signal = signal_store.get(signal_id)
        if signal.account_id != account_id:
            raise ValueError("ACCOUNT_CONTEXT_MISMATCH")
        return signal_store.create_revision(signal_id, policy_version=policy_version, limits=limits, context_revision=context_revision)
