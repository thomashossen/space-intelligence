"""Pipeline records.

RawItem   what a connector returns: the document as delivered, no business logic.
Item      the canonical record, filled stage by stage (normalise → … → score).
EventRec  a cluster of independent origins reporting the same fact.
SignalRec an observable statistical deviation, with its evidence and confidence.

Every field exists because a later stage, the API or the UI needs it; see
``docs`` in the README for the field-by-field justification.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawItem:
    """Output contract of every connector. Strings are kept exactly as received."""

    source: str                       # source id from the registry
    source_type: str
    url: str
    title: str
    summary: str
    published_at: str                 # raw date string, parsed later by dates.py
    author: str
    language: str                     # declared by the feed, may be empty
    raw_content: str                  # full payload text/HTML of the entry, never modified
    extra: dict[str, Any] = field(default_factory=dict)   # connector provenance (publisher, ids)


@dataclass
class Item:
    # identity and provenance
    id: str
    raw_id: str
    source_id: str
    source_type: str
    family: str
    tier: int
    source_url: str                   # the feed / API endpoint the item came from
    url: str                          # as received
    canonical_url: str
    url_flags: list[str] = field(default_factory=list)
    publisher: str = ""               # real publisher behind an aggregator entry
    # text
    title: str = ""
    summary: str = ""
    cleaned_content: str = ""
    author: str = ""
    language: str = "und"
    language_declared: str = ""
    language_confidence: float = 0.0
    # time (ISO 8601, UTC)
    published_at: str = ""
    collected_at: str = ""
    updated_at: str = ""
    # enrichment
    countries: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    programmes: list[str] = field(default_factory=list)
    mentions: list[dict] = field(default_factory=list)    # entity spans with method
    sectors: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    classification: dict = field(default_factory=dict)    # label -> evidence
    event_type: str = "other"
    event_type_evidence: list[str] = field(default_factory=list)
    numeric_facts: list[dict] = field(default_factory=list)
    # dedup / clustering
    duplicate_group_id: str = ""
    duplicate_of: str = ""
    dedup_level: str = ""
    dedup_similarity: float | None = None
    is_origin_head: bool = True
    event_id: str = ""
    # scores (0-100) and their components
    importance_score: int = 0
    credibility_score: int = 0
    novelty_score: int = 0
    strategic_score: int = 0
    geopolitical_score: int = 0
    industrial_score: int = 0
    data_quality_score: int = 0
    explanation: dict = field(default_factory=dict)
    embedding: list[float] | None = None
    # processing
    flags: list[str] = field(default_factory=list)
    processing_status: str = "new"    # processed | out_of_scope | rejected
    processing_version: str = ""
    rules_version: str = ""
    model_version: str = ""
    processed_at: str = ""
    data_origin: str = "fixture"      # fixture | live

    @property
    def text(self) -> str:
        return f"{self.title}. {self.summary} {self.cleaned_content}".strip()

    @property
    def entity_ids(self) -> list[str]:
        return self.companies + self.organizations + self.technologies + self.programmes


@dataclass
class EventRec:
    event_id: str
    title: str
    description: str
    event_type: str
    event_date: str
    first_seen: str
    last_seen: str
    item_ids: list[str]
    origin_ids: list[str]             # duplicate_group_ids
    head_ids: list[str]               # one item per origin
    source_ids: list[str]
    families: list[str]
    companies: list[str] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    programmes: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    primary_source: bool = False
    contradictions: list[dict] = field(default_factory=list)
    status: str = "candidate"         # candidate | confirmed | contested
    epistemic_status: str = "FACT"    # observed and reported; status + confidence qualify it
    confidence_level: str = "low"
    confidence_rules: list[dict] = field(default_factory=list)
    importance: int = 0
    credibility: int = 0
    novelty: int = 0
    strategic: int = 0
    geopolitical: int = 0
    industrial: int = 0
    relevance: int = 0
    explanation: dict = field(default_factory=dict)

    @property
    def entity_ids(self) -> list[str]:
        return self.companies + self.organizations + self.technologies + self.programmes


@dataclass
class SignalRec:
    signal_id: str
    kind: str                         # acceleration | surge | drop | new_entity | new_relation
    subject_type: str                 # entity type, country or topic
    subject_id: str
    subject_label: str
    statement: str                    # observable, factual sentence
    baseline: float | None
    current: float
    deviation: float | None           # current − baseline
    z_score: float | None
    growth_rate: float | None
    rolling_average: float | None
    activity_acceleration: float | None
    p_value: float | None
    series: list[int]
    evidence: list[str]               # event ids of the current window
    evidence_count: int
    independent_origins: int
    families: list[str]
    confidence_score: int
    confidence_level: str
    confidence_components: list[dict]
    caveats: list[str]
    window_start: str
    window_end: str
    epistemic_status: str = "SIGNAL"


@dataclass
class LogEntry:
    stage: str
    level: str                        # info | warning | error
    code: str
    message: str
    source_id: str = ""
    item_id: str = ""


@dataclass
class StageMetrics:
    name: str
    counts: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    errors: int = 0
    _t0: float = 0.0

    def start(self) -> "StageMetrics":
        self._t0 = time.perf_counter()
        return self

    def stop(self) -> "StageMetrics":
        self.seconds = round(time.perf_counter() - self._t0, 4)
        return self

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n

    def as_dict(self) -> dict:
        return {"stage": self.name, "seconds": self.seconds, "errors": self.errors, **self.counts}
