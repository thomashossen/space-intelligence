"""CLASSIFY: multi-label taxonomy + event type, from taxonomy.toml.

Rules, not a model: every label carries the keywords and entities that produced
it, so "why is this tagged SUPPLY_CHAIN?" always has an answer. An item with no
A&D label at all is marked ``out_of_scope``: it stays in the database (traceable)
but feeds no event, score or signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.pipeline.entities import Gazetteer


def _kw_pattern(keyword: str) -> re.Pattern:
    kw = keyword.strip().lower()
    if kw.endswith("*"):
        body = re.escape(kw[:-1]) + r"[\w-]*"
    else:
        body = re.escape(kw)
    return re.compile(rf"(?<![\w]){body}(?![\w])")


@dataclass
class Category:
    label: str
    kind: str
    keywords: list[tuple[str, re.Pattern]]
    entities: list[str]


class Classifier:
    def __init__(self, taxonomy: dict, gaz: Gazetteer):
        s = taxonomy.get("settings", {})
        self.threshold = s.get("threshold", 2)
        self.w_title = s.get("title_weight", 2)
        self.w_body = s.get("body_weight", 1)
        self.w_entity = s.get("entity_weight", 2)
        self.version = str(taxonomy.get("version", ""))
        self.gaz = gaz
        self.categories = [
            Category(label, c.get("kind", "topic"), [(k, _kw_pattern(k)) for k in c.get("keywords", [])], c.get("entities", []))
            for label, c in taxonomy.get("category", {}).items()
        ]
        self.event_types = [(name, [(k, _kw_pattern(k)) for k in kws]) for name, kws in taxonomy.get("event_types", {}).items()]

    def _entity_hits(self, cat: Category, entity_ids: set[str], source_type: str) -> list[str]:
        hits = []
        for ref in cat.entities:
            if ref.startswith("type:"):
                parts = ref.split(":")
                if parts[1] == "research_source":
                    if source_type == "research":
                        hits.append(ref)
                    continue
                etype, sector = parts[1], (parts[2] if len(parts) > 2 else "")
                if any(self.gaz.entities[e].type == etype and (not sector or self.gaz.entities[e].sector == sector)
                       for e in entity_ids if e in self.gaz.entities):
                    hits.append(ref)
            elif ref in entity_ids:
                hits.append(ref)
        return hits

    def classify(self, item) -> None:
        title = item.title.lower()
        body = f"{item.summary}\n{item.cleaned_content}".lower()
        entity_ids = set(item.entity_ids) | {f"country-{c.lower()}" for c in item.countries}
        entity_ids |= {m["entity_id"] for m in item.mentions}
        result: dict[str, dict] = {}
        for cat in self.categories:
            kw_title = [k for k, p in cat.keywords if p.search(title)]
            kw_body = [k for k, p in cat.keywords if p.search(body)]
            ents = self._entity_hits(cat, entity_ids, item.source_type)
            score = self.w_title * len(kw_title) + self.w_body * len(kw_body) + self.w_entity * len(ents)
            if score >= self.threshold:
                result[cat.label] = {"kind": cat.kind, "score": score, "title_keywords": kw_title,
                                     "body_keywords": kw_body, "entities": ents}
        item.classification = result
        item.sectors = [k for k, v in result.items() if v["kind"] == "sector"]
        item.topics = [k for k, v in result.items() if v["kind"] != "sector"]

        best, best_score, evidence = "other", 0, []
        for name, pats in self.event_types:
            t = [k for k, p in pats if p.search(title)]
            b = [k for k, p in pats if p.search(body)]
            score = 2 * len(t) + len(b)
            if score > best_score:
                best, best_score, evidence = name, score, [f"title:{k}" for k in t] + [f"body:{k}" for k in b]
        if item.source_type == "research" and best not in ("research",):
            evidence.append("source_type:research")
            best = "research"
        item.event_type, item.event_type_evidence = best, evidence
        # In scope = an A&D label AND something concrete to anchor it: a catalogued entity,
        # or at least 3 distinct keywords (one repeated word is not enough).
        distinct = {k for v in result.values() for k in v["title_keywords"] + v["body_keywords"]}
        relevant = bool(item.sectors) or len(item.topics) >= 2
        anchored = bool(item.entity_ids) or len(distinct) >= 3
        item.processing_status = "processed" if relevant and anchored else "out_of_scope"
        if relevant and not anchored:
            item.flags = sorted(set(item.flags) | {"no_entity"})
