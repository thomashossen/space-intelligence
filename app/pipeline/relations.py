"""RELATIONS: an incremental, evidence-backed entity graph.

Two methods, always recorded:
  cooccurrence  two entities in the same EVENT (not the same article). Edge type
                follows the entity types, oriented company → technology →
                programme → country. Weak by construction (confidence low until
                3 events).
  pattern       an explicit verb between two entity mentions in one sentence of
                an origin head ("ESA selects Isar Aerospace", "X acquires Y").

Not a knowledge base: no inference, no transitivity. Every edge lists its events.
"""

from __future__ import annotations

import hashlib
import re
from itertools import combinations

from app.pipeline.models import EventRec, Item
from app.pipeline.text import sentences

ORDER = {"company": 0, "organization": 0, "technology": 1, "material": 1, "programme": 2, "country": 3, "region": 3}
COOC_TYPES = {
    (0, 0): "co_mentioned", (0, 1): "works_on", (0, 2): "involved_in", (0, 3): "active_in",
    (1, 1): "co_mentioned", (1, 2): "used_in", (1, 3): "active_in", (2, 2): "co_mentioned", (2, 3): "involves_country",
}
PATTERNS = [
    ("contracts", re.compile(r"\b(awards?|awarded|selects?|selected|orders?|ordered|chooses|chose|commande|retient|attribue)\b", re.I)),
    ("partners_with", re.compile(r"\b(partners? with|teams? up with|signs? (?:an? )?(?:agreement|memorandum|MoU) with|joins forces with|s'associe à)\b", re.I)),
    ("acquires", re.compile(r"\b(acquires?|acquired|to acquire|buys|bought|takes over|rachète)\b", re.I)),
    ("supplies", re.compile(r"\b(supplies|to supply|delivers?|delivered|livre)\b", re.I)),
]


def _rid(a: str, b: str, t: str) -> str:
    return "rel-" + hashlib.sha1(f"{a}|{b}|{t}".encode()).hexdigest()[:12]


def _orient(a: str, b: str, types: dict[str, str]) -> tuple[str, str]:
    oa, ob = ORDER.get(types.get(a, ""), 9), ORDER.get(types.get(b, ""), 9)
    return (a, b) if (oa, a) <= (ob, b) else (b, a)


def build_relations(events: list[EventRec], heads_by_event: dict[str, list[Item]], gaz) -> list[dict]:
    types = {eid: e.type for eid, e in gaz.entities.items()}
    country_ids = {e.country: eid for eid, e in gaz.entities.items() if e.type in ("country", "region")}
    rels: dict[str, dict] = {}

    def add(a, b, rtype, method, ev: EventRec, evidence: str = ""):
        rid = _rid(a, b, rtype)
        r = rels.setdefault(rid, {"relation_id": rid, "source_entity": a, "target_entity": b, "relation_type": rtype,
                                  "method": method, "events": [], "first_seen": ev.first_seen, "last_seen": ev.first_seen,
                                  "evidence": []})
        if ev.event_id not in r["events"]:
            r["events"].append(ev.event_id)
        r["first_seen"] = min(r["first_seen"], ev.first_seen)
        r["last_seen"] = max(r["last_seen"], ev.first_seen)
        if evidence and len(r["evidence"]) < 5:
            r["evidence"].append(evidence)

    for ev in sorted(events, key=lambda e: e.first_seen):
        nodes = list(dict.fromkeys(ev.entity_ids + [country_ids[c] for c in ev.countries if c in country_ids]))
        for x, y in combinations(nodes, 2):
            a, b = _orient(x, y, types)
            ka, kb = ORDER.get(types.get(a, ""), 9), ORDER.get(types.get(b, ""), 9)
            rtype = COOC_TYPES.get((ka, kb))
            if rtype:
                add(a, b, rtype, "cooccurrence", ev)
        for h in heads_by_event.get(ev.event_id, []):
            for sent in sentences(f"{h.title}. {h.summary}")[:6]:
                found = [m for m in gaz.find(sent) if m.entity_type in ("company", "organization")]
                for m1, m2 in zip(found, found[1:]):
                    if m1.entity_id == m2.entity_id:
                        continue
                    between = sent[m1.end:m2.start]
                    for rtype, pat in PATTERNS:
                        if pat.search(between) and len(between) < 80:
                            add(m1.entity_id, m2.entity_id, rtype, "pattern", ev, sent[:240])
                            break
    out = []
    for r in rels.values():
        n = len(r["events"])
        r["weight"] = n
        r["confidence"] = "moderate" if r["method"] == "pattern" or n >= 3 else "low"
        r["events"] = r["events"][-20:]
        out.append(r)
    return sorted(out, key=lambda r: r["relation_id"])
