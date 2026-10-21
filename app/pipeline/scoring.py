"""SCORE: credibility, importance, novelty, strategic, data quality.

Every score returns ``(value, explanation)`` where the explanation lists each
component with its value, points and a sentence saying where it comes from. The
explanation is stored with the row; the API serves it as-is. Weights and points
come from scoring.toml (``rules_version``).

Credibility ≠ data quality: credibility asks "how much can we trust what this
says?", data quality asks "how complete and consistent is this record?".
"""

from __future__ import annotations

import re
from datetime import datetime
from itertools import combinations

from app.pipeline.config import PipelineConfig, SourceConfig
from app.pipeline.dates import from_iso
from app.pipeline.models import EventRec, Item
from app.pipeline.similarity import TfIdf
from app.pipeline.stats import Activity, series_stats, week_index


def _c(key: str, label: str, value: float, note: str, weight: float | None = None) -> dict:
    out = {"key": key, "label": label, "value": round(value, 1), "note": note}
    if weight is not None:
        out["weight"] = weight
        out["contribution"] = round(weight * value, 1)
    return out


def _clamp(v: float) -> int:
    return int(max(0, min(100, round(v))))


# --------------------------------------------------------------------------- credibility

def credibility(baseline: float, *, primary: bool, origins: int, families: int, reliable_date_share: float,
                contradictions: int, cfg: dict, source_label: str) -> tuple[int, dict]:
    c = cfg["credibility"]
    rel = c["reliability_points"] * baseline
    prim = c["primary_bonus"] if primary else 0
    corr = min(c["corroboration_max"],
               c["corroboration_per_origin"] * max(0, origins - 1) + c["corroboration_per_family"] * max(0, families - 1))
    temp = c["temporal_points"] * reliable_date_share
    contra = min(c["contradiction_max"], c["contradiction_penalty"] * contradictions)
    comps = [
        _c("source_reliability", "Source reliability", rel, f"{c['reliability_points']} × baseline {baseline:.2f} ({source_label})"),
        _c("primary_source_bonus", "Primary source", prim, "published by the actor or authority itself" if primary else "no primary source"),
        _c("corroboration_bonus", "Corroboration", corr, f"{origins} independent origin(s), {families} famil(y/ies) after deduplication"),
        _c("temporal_consistency", "Temporal consistency", temp, f"{round(100 * reliable_date_share)} % of origins with a reliable publication date"),
        _c("contradiction_penalty", "Contradictions", -contra, f"{contradictions} numeric disagreement(s) between origins" if contradictions else "none detected"),
    ]
    value = _clamp(rel + prim + corr + temp - contra)
    return value, {"value": value, "formula": "source_reliability + primary_source_bonus + corroboration_bonus + temporal_consistency − contradiction_penalty",
                   "components": comps}


# --------------------------------------------------------------------------- relevance / strategic

def relevance(sectors: list[str], topics: list[str], cfg: dict) -> tuple[int, dict]:
    r = cfg["relevance"]
    s, t = min(2, len(sectors)), min(4, len(topics))
    value = _clamp(r["per_sector"] * s + r["per_topic"] * t)
    return value, {"value": value, "components": [
        _c("sectors", "A&D sector labels", r["per_sector"] * s, ", ".join(sectors) or "none"),
        _c("topics", "Topic labels", r["per_topic"] * t, ", ".join(topics) or "none"),
    ]}


def _entity_keys(ev: EventRec) -> set[str]:
    keys = set(ev.entity_ids)
    for c in ev.countries:
        keys.add("region-eu" if c == "EU" else f"country-{c.lower()}")
    return keys


def strategic(ev: EventRec, text: str, entity_types: dict[str, str], cfg: dict) -> tuple[int, dict]:
    s = cfg["strategic"]
    labels = set(ev.sectors + ev.topics)
    keys = _entity_keys(ev)
    components = {}
    for name, weight in s["weights"].items():
        rules = s.get(name, {})
        triggers, points = [], 0.0
        for label, p in rules.get("categories", {}).items():
            if label in labels:
                points += p
                triggers.append(f"{label} +{p}")
        for eid, p in rules.get("entities", {}).items():
            if eid in keys:
                points += p
                triggers.append(f"{eid} +{p}")
        for etype, p in rules.get("entity_types", {}).items():
            n = sum(1 for e in ev.entity_ids if entity_types.get(e) == etype)
            if n:
                points += p * n
                triggers.append(f"{n} {etype} × {p}")
        if rules.get("per_company") and ev.companies:
            points += rules["per_company"] * len(ev.companies)
            triggers.append(f"{len(ev.companies)} companies × {rules['per_company']}")
        if rules.get("per_extra_country") and len(ev.countries) > 1:
            points += rules["per_extra_country"] * (len(ev.countries) - 1)
            triggers.append(f"{len(ev.countries)} countries")
        if ev.event_type in rules.get("event_types", {}):
            p = rules["event_types"][ev.event_type]
            points += p
            triggers.append(f"event type {ev.event_type} +{p}")
        kws = [k for k in rules.get("keywords", []) if re.search(r"(?<!\w)" + re.escape(k.rstrip("*")) + (r"\w*" if k.endswith("*") else "") + r"(?!\w)", text, re.I)]
        if kws:
            points += rules.get("keyword_points", 0)
            triggers.append(f"keywords {', '.join(kws)} +{rules.get('keyword_points', 0)}")
        components[name] = (_clamp(points), triggers)
    value = _clamp(sum(s["weights"][k] * v for k, (v, _) in components.items()))
    return value, {"value": value, "formula": "Σ weight × component (each component 0-100 from rules)",
                   "components": [_c(k, k.replace("_", " ").capitalize(), v, "; ".join(t) or "no rule triggered", s["weights"][k])
                                  for k, (v, t) in components.items()]}


# --------------------------------------------------------------------------- importance

def importance(*, best_baseline: float, origins: int, relevance_value: int, first_seen: str, as_of: datetime,
               impact: int, cfg: dict, date_reliable: bool = True) -> tuple[int, dict]:
    w = cfg["importance"]
    age_h = max(0.0, (as_of - from_iso(first_seen)).total_seconds() / 3600)
    recency = 100 * 0.5 ** (age_h / w["recency_half_life_hours"]) if date_reliable else 50.0
    recency_note = (f"first seen {round(age_h)} h before the run's as-of time, half-life {w['recency_half_life_hours']} h"
                    if date_reliable else "publication date unknown (estimated): neutral value 50")
    comps = [
        _c("reliability", "Source reliability", 100 * best_baseline, "best baseline credibility among origins", w["reliability"]),
        _c("origins", "Independent origins", min(100, 20 + 20 * origins), f"{origins} origin(s): 20 + 20 per origin, capped at 100", w["origins"]),
        _c("relevance", "Sector relevance", relevance_value, "taxonomy labels (see classification)", w["relevance"]),
        _c("recency", "Recency", recency, recency_note, w["recency"]),
        _c("impact", "Potential impact", impact, "strategic score (see its components)", w["impact"]),
    ]
    value = _clamp(sum(c["contribution"] for c in comps))
    return value, {"value": value, "formula": "Σ weight × component", "components": comps,
                   "priority": value >= w["priority_threshold"], "as_of": as_of.isoformat()}


# --------------------------------------------------------------------------- novelty

class NoveltyContext:
    """Chronological knowledge needed to say what is new: first sightings and similarity to the past."""

    def __init__(self, events: list[EventRec], activity: Activity, entity_types: dict[str, str]):
        self.events = sorted(events, key=lambda e: (e.first_seen, e.event_id))
        self.activity = activity
        self.entity_types = entity_types
        self.pair_first: dict[tuple[str, str], str] = {}
        for ev in self.events:
            for pair in combinations(sorted(set(ev.entity_ids)), 2):
                self.pair_first.setdefault(pair, ev.event_id)
        self.tfidf = TfIdf({e.event_id: f"{e.title}. {e.description}" for e in self.events})
        self.window_start = self.events[0].first_seen if self.events else ""

    def stats_at(self, subject: tuple[str, str], week: int):
        series = self.activity.counts.get(subject, [0] * self.activity.weeks)[: week + 1]
        return series_stats(series) if len(series) >= 5 else None


def novelty(ev: EventRec, ctx: NoveltyContext, cfg: dict, as_of: datetime) -> tuple[int, dict]:
    n = cfg["novelty"]
    min_hist = cfg["signals"]["min_history_weeks"]
    week = week_index(from_iso(ev.first_seen), as_of, ctx.activity.weeks)
    history_ok = ctx.activity.history_weeks >= min_hist and week is not None and week >= 4
    comps, notes = [], []

    new_topics = []
    if history_ok:
        for t in ev.topics + ev.sectors:
            st = ctx.stats_at(("topic", t), week)
            if st and st.baseline_mean < n["new_topic_baseline"]:
                new_topics.append(t)
    comps.append(_c("new_topic", "New topic", n["new_topic"] if new_topics else 0,
                    ("rare before: " + ", ".join(new_topics)) if new_topics else ("no rare topic" if history_ok else "not assessed: insufficient history")))

    def first_mentions(types: tuple[str, ...]) -> list[str]:
        return [e for e in ev.entity_ids if ctx.entity_types.get(e) in types and ctx.activity.first_seen.get(e) == ev.first_seen]

    actors = first_mentions(("company", "organization")) if history_ok else []
    techs = first_mentions(("technology", "material")) if history_ok else []
    comps.append(_c("new_actor", "New actor", n["new_actor"] if actors else 0,
                    ("first mention in the window: " + ", ".join(actors)) if actors else ("no new actor" if history_ok else "not assessed: insufficient history")))
    comps.append(_c("new_technology", "New technology", n["new_technology"] if techs else 0,
                    ("first mention: " + ", ".join(techs)) if techs else "no new technology"))

    new_pairs = [p for p in combinations(sorted(set(ev.entity_ids)), 2) if ctx.pair_first.get(p) == ev.event_id
                 and not (p[0] in actors or p[1] in actors)] if history_ok else []
    comps.append(_c("new_relation", "New relation", n["new_relation"] if new_pairs else 0,
                    ("first co-occurrence: " + "; ".join(f"{a} × {b}" for a, b in new_pairs[:3])) if new_pairs else "no new pair"))

    best_z, best_subj = 0.0, ""
    if week is not None and week >= 4:
        for subj in set([(ctx.entity_types.get(e, "entity"), e) for e in ev.entity_ids] + [("topic", t) for t in ev.topics]):
            st = ctx.stats_at(subj, week)
            if st and st.z > best_z and st.current >= 2:
                best_z, best_subj = st.z, subj[1]
    freq = min(n["frequency_change_max"], n["frequency_change_per_z"] * best_z)
    comps.append(_c("frequency_change", "Frequency change", freq,
                    f"max z-score {best_z:.1f} ({best_subj})" if best_subj else "no subject above its weekly baseline"))

    prior = [e for e in ctx.events if e.first_seen < ev.first_seen
             and (from_iso(ev.first_seen) - from_iso(e.first_seen)).days <= n["lookback_days"]]
    max_sim = max((ctx.tfidf.cosine(ev.event_id, e.event_id) for e in prior), default=0.0)
    dis = n["dissimilarity_points"] * (1 - max_sim)
    comps.append(_c("dissimilarity", "Distance to earlier events", dis,
                    f"1 − {max_sim:.2f} (closest of {len(prior)} earlier events, {n['lookback_days']} days)" if prior else "no earlier event in the window"))
    if not history_ok:
        notes.append("History shorter than required: new-topic and new-actor components not assessed.")
    value = _clamp(sum(c["value"] for c in comps))
    return value, {"value": value, "formula": "sum of components; computed once per event, reprints add nothing",
                   "components": comps, "notes": notes}


# --------------------------------------------------------------------------- data quality

def data_quality(item: Item, cfg: dict) -> tuple[int, dict]:
    q = cfg["data_quality"]
    n = len(item.cleaned_content)
    comps = [
        _c("title", "Title", q["title"] if item.title and "title_derived" not in item.flags else q["title"] / 2 if item.title else 0,
           "derived from the text" if "title_derived" in item.flags else "present"),
        _c("content", "Content", q["content"] if n >= 400 else q["content"] / 2 if n >= 120 else 0,
           f"{n} characters of cleaned text" + (" (metadata only)" if "metadata_only" in item.flags else "")),
        _c("reliable_date", "Publication date", 0 if "date_estimated" in item.flags else q["reliable_date"],
           "estimated (collection time)" if "date_estimated" in item.flags else "parsed from the source"),
        _c("known_language", "Language", q["known_language"] if item.language != "und" else 0, item.language),
        _c("author", "Author", q["author"] if item.author else 0, item.author or "missing"),
        _c("canonical_url", "Canonical URL", q["canonical_url"] if item.canonical_url and not {"not_absolute", "unparseable", "empty_url"} & set(item.url_flags) else 0,
           ", ".join(item.url_flags) or "clean"),
        _c("entities", "Entities found", q["entities"] if item.entity_ids else 0, f"{len(item.entity_ids)} entities"),
        _c("classified", "Classified", q["classified"] if item.sectors or item.topics else 0, ", ".join(item.sectors + item.topics) or "no label"),
    ]
    value = _clamp(sum(c["value"] for c in comps))
    return value, {"value": value, "formula": "sum of completeness and consistency checks", "components": comps}


# --------------------------------------------------------------------------- orchestration

def score_event(ev: EventRec, heads: list[Item], sources: dict[str, SourceConfig], cfg: PipelineConfig,
                ctx: NoveltyContext, as_of: datetime) -> None:
    rules = cfg.scoring
    baselines = {h.source_id: cfg.baseline(sources[h.source_id]) for h in heads}
    best_src = max(heads, key=lambda h: (baselines[h.source_id], -h.tier))
    reliable = sum(1 for h in heads if "date_estimated" not in h.flags) / len(heads)
    ev.credibility, cred = credibility(
        baselines[best_src.source_id], primary=ev.primary_source, origins=len(heads), families=len(ev.families),
        reliable_date_share=reliable, contradictions=len(ev.contradictions), cfg=rules,
        source_label=f"tier {best_src.tier}, {sources[best_src.source_id].name}")
    text = " ".join(f"{h.title}. {h.summary}" for h in heads)
    ev.strategic, strat = strategic(ev, text, ctx.entity_types, rules)
    ev.geopolitical = next(c["value"] for c in strat["components"] if c["key"] == "geopolitical")
    ev.industrial = next(c["value"] for c in strat["components"] if c["key"] == "industrial")
    ev.relevance, rel = relevance(ev.sectors, ev.topics, rules)
    ev.importance, imp = importance(best_baseline=baselines[best_src.source_id], origins=len(heads), relevance_value=ev.relevance,
                                    first_seen=ev.first_seen, as_of=as_of, impact=ev.strategic, cfg=rules,
                                    date_reliable=reliable > 0)
    ev.novelty, nov = novelty(ev, ctx, rules, as_of)
    ev.explanation.update({"credibility": cred, "strategic": strat, "relevance": rel, "importance": imp, "novelty": nov})


def score_item(item: Item, ev: EventRec | None, src: SourceConfig, cfg: PipelineConfig) -> None:
    rules = cfg.scoring
    item.data_quality_score, dq = data_quality(item, rules)
    expl = {"data_quality": dq}
    if ev is None:
        item.explanation = expl
        return
    own_primary = bool(src.publisher_entity) and src.publisher_entity in set(ev.entity_ids)
    item.credibility_score, cred = credibility(
        cfg.baseline(src), primary=own_primary, origins=len(ev.origin_ids), families=len(ev.families),
        reliable_date_share=0.0 if "date_estimated" in item.flags else 1.0, contradictions=len(ev.contradictions),
        cfg=rules, source_label=f"tier {src.tier}, {src.name}")
    item.importance_score = ev.importance
    item.strategic_score, item.geopolitical_score, item.industrial_score = ev.strategic, ev.geopolitical, ev.industrial
    if item.is_origin_head and item.id == ev.head_ids[0]:
        item.novelty_score = ev.novelty
        nov_note = "first origin of the event: carries the event's novelty"
    elif item.is_origin_head:
        item.novelty_score = round(ev.novelty / 2)
        nov_note = "later independent origin of the same event: half the event's novelty"
    else:
        item.novelty_score = 0
        nov_note = f"reprint of {item.duplicate_of} ({item.dedup_level}): adds no novelty"
    expl.update({"credibility": cred, "novelty": {"value": item.novelty_score, "note": nov_note, "event": ev.event_id},
                 "importance": {"value": ev.importance, "note": "importance is an event property", "event": ev.event_id}})
    item.explanation = expl
