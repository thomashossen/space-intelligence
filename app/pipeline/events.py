"""CLUSTER: origins → events. An article is not an event.

Only origin heads (one per duplicate group) are compared, so reprints can never
inflate an event. Two heads are linked when they are within ``window_hours``,
share at least one non-country entity, and their similarity reaches the
threshold:

    similarity = w_text · TF-IDF cosine + w_entities · IDF-weighted entity Jaccard + w_title · title Jaccard

The publisher entity (an MBDA release "mentions" MBDA by provenance) is not content
and is left out; entities present in many events weigh less (IDF).

Cross-language pairs (FR↔EN) cannot use text similarity; they are linked on
entities only, with a stricter rule (≥ 2 shared entities, same known event type).
Clustering is greedy and chronological with a mean-link guard, so one ambiguous
article cannot chain two unrelated stories together. Each link records its
similarity breakdown in the event explanation.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from datetime import timedelta

from app.pipeline.dates import from_iso
from app.pipeline.models import EventRec, Item
from app.pipeline.similarity import TfIdf, jaccard, tokens

UNITS = {
    "mission": "missions", "missions": "missions", "launch": "launches", "launches": "launches",
    "satellite": "satellites", "satellites": "satellites", "aircraft": "aircraft", "jet": "jets",
    "jets": "jets", "unit": "units", "units": "units", "system": "systems", "systems": "systems",
    "drone": "drones", "drones": "drones", "missile": "missiles", "missiles": "missiles",
    "interceptor": "interceptors", "interceptors": "interceptors", "radar": "radars", "radars": "radars",
    "launcher": "launchers", "launchers": "launchers", "vehicle": "vehicles", "vehicles": "vehicles",
    "lancement": "launches", "lancements": "launches", "systèmes": "systems", "système": "systems",
}
NUM_FACT = re.compile(r"\b(\d{1,4}(?:[.,]\d+)?)\s+(?:[a-zA-Zéè-]+\s+)?(" + "|".join(sorted(UNITS, key=len, reverse=True)) + r")\b", re.I)
MONEY = re.compile(r"(€|\$|£|EUR|USD)\s?(\d+(?:[.,]\d+)?)\s?(bn|billion|m|million|millions|milliards?)\b", re.I)


def numeric_facts(item: Item) -> list[dict]:
    """Numbers stated in the title and lead (first two sentences), with their unit."""
    lead = item.title + ". " + " ".join(re.split(r"(?<=[.!?])\s+", item.summary)[:2])
    facts = []
    for value, unit in NUM_FACT.findall(lead):
        facts.append({"unit": UNITS[unit.lower()], "value": float(value.replace(",", "."))})
    for cur, value, scale in MONEY.findall(lead):
        mult = 1000 if scale.lower().startswith(("bn", "billion", "milliard")) else 1
        facts.append({"unit": f"amount_{cur.upper().replace('€', 'EUR').replace('$', 'USD').replace('£', 'GBP')}_m",
                      "value": float(value.replace(",", ".")) * mult})
    return facts


def _entities(item: Item) -> set[str]:
    """Entities found in the TEXT. The publisher entity (provenance, not content) is excluded."""
    publisher_only = {m["entity_id"] for m in item.mentions if m.get("method") == "publisher" and m.get("count", 1) == 1}
    return set(item.companies + item.organizations + item.technologies + item.programmes) - publisher_only


def weighted_jaccard(a: set[str], b: set[str], idf: dict[str, float]) -> float:
    """Jaccard where each entity weighs its IDF: sharing "Airbus" in an Airbus newsroom says little."""
    union = a | b
    if not union:
        return 0.0
    return sum(idf.get(e, 1.0) for e in a & b) / sum(idf.get(e, 1.0) for e in union)


def pair_similarity(a: Item, b: Item, tfidf: TfIdf, rules: dict, idf: dict[str, float] | None = None) -> tuple[float, dict]:
    ea, eb = _entities(a), _entities(b)
    shared = ea & eb
    if not shared:
        return 0.0, {"reason": "no shared entity"}
    ent_j = weighted_jaccard(ea, eb, idf or {})
    known = {"und", ""}
    if a.language not in known and b.language not in known and a.language != b.language:
        same_type = a.event_type == b.event_type and a.event_type != "other"
        ok = ent_j >= rules["cross_language_entity_jaccard"] and len(shared) >= rules["cross_language_min_shared"] and same_type
        return (ent_j if ok else 0.0), {"cross_language": True, "entity_jaccard": round(ent_j, 3),
                                       "shared": sorted(shared), "same_event_type": same_type}
    text = tfidf.cosine(a.id, b.id)
    title = jaccard(set(tokens(a.title)), set(tokens(b.title)))
    sim = rules["w_text"] * text + rules["w_entities"] * ent_j + rules["w_title"] * title
    return sim, {"text_cosine": round(text, 3), "entity_jaccard": round(ent_j, 3), "title_jaccard": round(title, 3),
                 "shared": sorted(shared)}


def conflicts(a: Item, b: Item) -> list[str]:
    """Reasons to doubt that two items describe the same event."""
    out = []
    if a.event_type != b.event_type and "other" not in (a.event_type, b.event_type):
        out.append(f"event types differ ({a.event_type} / {b.event_type})")
    ca, cb = set(a.companies), set(b.companies)
    if ca and cb and not ca & cb:
        out.append("no company in common")
    pa, pb = set(a.programmes), set(b.programmes)
    if pa and pb and not pa & pb:
        out.append("no programme in common")
    na, nb = set(a.countries) - {"EU", "INT"}, set(b.countries) - {"EU", "INT"}
    if na and nb and not na & nb:
        out.append("no country in common")
    return out


def _threshold(a: Item, b: Item, rules: dict) -> float:
    return rules["different_type_threshold"] if conflicts(a, b) else rules["threshold"]


def cluster(heads: list[Item], rules: dict) -> tuple[list[list[Item]], dict[str, dict]]:
    """Return clusters of heads and, per joined head, the link that attached it."""
    window = timedelta(hours=rules["window_hours"])
    order = sorted(heads, key=lambda h: (h.published_at, h.tier, h.id))
    tfidf = TfIdf({h.id: f"{h.title}. {h.summary} {h.cleaned_content[:3000]}" for h in order})
    df = Counter(e for h in order for e in _entities(h))
    n = max(1, len(order))
    idf = {e: math.log((1 + n) / (1 + d)) + 1.0 for e, d in df.items()}
    clusters: list[dict] = []
    links: dict[str, dict] = {}
    for h in order:
        t = from_iso(h.published_at)
        best = None
        for c in clusters:
            if t - c["first"] > window:      # an event spans at most window_hours from its first report
                continue
            scored = [(pair_similarity(h, m, tfidf, rules, idf), m) for m in c["members"]]
            mean = sum(s[0][0] for s in scored) / len(scored)
            if mean < rules["threshold"] * rules["mean_link_ratio"]:
                continue
            # Link through the most similar member whose own threshold is met.
            passing = [(sim, detail, m, _threshold(h, m, rules)) for (sim, detail), m in scored if sim >= _threshold(h, m, rules)]
            if not passing:
                continue
            top_sim, detail, top_m, thr = max(passing, key=lambda p: p[0])
            if best is None or top_sim > best[0]:
                best = (top_sim, c, top_m, detail, thr)
        if best:
            sim, c, m, detail, thr = best
            c["members"].append(h)
            c["last"] = max(c["last"], t)
            links[h.id] = {"linked_to": m.id, "similarity": round(sim, 3), "threshold": thr,
                           "conflicts": conflicts(h, m), **detail}
        else:
            clusters.append({"members": [h], "first": t, "last": t})
    return [c["members"] for c in clusters], links


def event_id_for(members: list[Item]) -> str:
    first = min(members, key=lambda h: (h.published_at, h.id))
    return "ev-" + hashlib.sha1((first.canonical_url or first.id).encode()).hexdigest()[:10]


def _ranked(values: list[list[str]]) -> list[str]:
    c = Counter(v for vs in values for v in vs)
    return [k for k, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]


def contradictions(heads: list[Item], source_names: dict[str, str]) -> list[dict]:
    by_unit: dict[str, dict[float, list[Item]]] = {}
    for h in heads:
        seen = set()
        for f in h.numeric_facts:
            key = (f["unit"], f["value"])
            if key in seen:
                continue
            seen.add(key)
            by_unit.setdefault(f["unit"], {}).setdefault(f["value"], []).append(h)
    out = []
    for unit, values in sorted(by_unit.items()):
        if len(values) < 2:
            continue
        out.append({
            "fact": unit,
            "values": [
                {"value": v, "origins": len(hs), "sources": sorted({source_names.get(x.source_id, x.source_id) for x in hs}),
                 "best_tier": min(x.tier for x in hs)}
                for v, hs in sorted(values.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            ],
            "note": "Origins state different values in their title or lead.",
        })
    return out


def build_event(members: list[Item], groups: dict[str, list[Item]], links: dict[str, dict],
                source_cfg: dict, source_names: dict[str, str]) -> EventRec:
    heads = sorted(members, key=lambda h: (h.published_at, h.id))
    items = [it for h in heads for it in groups[h.duplicate_group_id]]
    rep = min(heads, key=lambda h: (h.tier, h.language != "en", h.published_at, h.id))
    weights = Counter()
    for h in heads:
        if h.event_type != "other":
            weights[h.event_type] += 5 - h.tier
    event_type = max(sorted(weights), key=lambda k: weights[k]) if weights else "other"
    ent_union = set(x for h in heads for x in _entities(h))
    primary = [h.source_id for h in heads
               if source_cfg[h.source_id].publisher_entity and source_cfg[h.source_id].publisher_entity in ent_union]
    published = sorted(it.published_at for it in items)
    ev = EventRec(
        event_id=event_id_for(heads),
        title=rep.title,
        description=rep.summary,
        event_type=event_type,
        event_date=heads[0].published_at[:10],
        first_seen=published[0],
        last_seen=published[-1],
        item_ids=[it.id for it in items],
        origin_ids=[h.duplicate_group_id for h in heads],
        head_ids=[h.id for h in heads],
        source_ids=sorted({it.source_id for it in items}),
        families=sorted({h.family for h in heads}),
        companies=_ranked([h.companies for h in heads]),
        organizations=_ranked([h.organizations for h in heads]),
        countries=_ranked([h.countries for h in heads]),
        technologies=_ranked([h.technologies for h in heads]),
        programmes=_ranked([h.programmes for h in heads]),
        sectors=_ranked([h.sectors for h in heads]),
        topics=_ranked([h.topics for h in heads]),
        primary_source=bool(primary),
    )
    ev.contradictions = contradictions(heads, source_names)
    orig, fam = len(heads), len(ev.families)
    rules = [
        {"rule": "At least 3 independent origins", "met": orig >= 3, "value": orig},
        {"rule": "At least 2 source families", "met": fam >= 2, "value": fam},
        {"rule": "At least one primary source (the actor or the authority itself)", "met": bool(primary), "value": primary},
        {"rule": "No disagreement between origins on a stated number", "met": not ev.contradictions,
         "value": [c["fact"] for c in ev.contradictions]},
    ]
    ev.confidence_rules = rules
    if ev.contradictions:
        ev.status, ev.confidence_level = "contested", "low"
    elif orig < 2:
        ev.status, ev.confidence_level = "candidate", "low"
    else:
        ev.status = "confirmed"
        ev.confidence_level = "high" if all(r["met"] for r in rules) else "moderate"
    ev.explanation["clustering"] = {
        "representative": rep.id,
        "links": {h.id: links[h.id] for h in heads if h.id in links},
        "rule": "heads within the window, sharing a non-country entity, similarity ≥ threshold",
    }
    ev.explanation["primary_sources"] = primary
    return ev
