"""DETECT SIGNALS: NEWS → EVENT → SIGNAL candidates, from observable counts only.

Kinds
  acceleration  weekly event count far above its 12-week baseline (z, growth)
  surge         a subject that was (almost) absent appears with ≥ N events
  drop          a regular subject (almost) disappears (Poisson tail probability)
  new_entity    first appearance of a catalogued actor/technology/programme
  new_relation  two actors linked for the first time (explicit verb or confirmed event)

A signal is a statement about COLLECTED EVENTS ("9 events this week vs 3.1/week"),
never a forecast. Its confidence says how solid the observation is (statistical
strength, support, independent corroboration), and its caveats say what could
make it an artefact (coverage change, short history, fixture data).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from app.pipeline.dates import iso
from app.pipeline.models import EventRec, Item, SignalRec
from app.pipeline.stats import Activity

ACTOR_TYPES = ("company", "organization")
ENTITY_TYPES = ("company", "organization", "technology", "material", "programme")


def _sid(*parts: str) -> str:
    return "sg-" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]


def _level(score: int, c: dict) -> str:
    return "high" if score >= c["high"] else "moderate" if score >= c["moderate"] else "low"


class SignalDetector:
    def __init__(self, cfg: dict, activity: Activity, events: dict[str, EventRec], heads: dict[str, list[Item]],
                 source_first_seen: dict[str, str], labels: dict[str, str], as_of: datetime, data_origin: str):
        self.s = cfg["signals"]
        self.c = self.s["confidence"]
        self.activity, self.events, self.heads = activity, events, heads
        self.source_first_seen = source_first_seen
        self.labels = labels
        self.as_of = as_of
        self.window_start = as_of - timedelta(days=7)
        self.data_origin = data_origin

    # ------------------------------------------------------------------ helpers
    def _corroboration(self, event_ids: list[str]) -> tuple[int, list[str], float]:
        heads = [h for e in event_ids for h in self.heads.get(e, [])]
        origins = {h.duplicate_group_id for h in heads}
        families = sorted({h.family for h in heads})
        new_sources = [h for h in heads if self.source_first_seen.get(h.source_id, "") >= iso(self.window_start)]
        share = len(new_sources) / len(heads) if heads else 0.0
        return len(origins), families, share

    def _confidence(self, *, z: float | None, event_ids: list[str], statistical: bool) -> tuple[int, list[dict], list[str], int, list[str]]:
        c = self.c
        origins, families, new_share = self._corroboration(event_ids)
        stat = min(c["z_max"], c["per_z"] * abs(z)) if (statistical and z is not None) else 0
        support = min(c["support_max"], c["per_event"] * len(event_ids))
        corr = min(c["corroboration_max"], c["per_origin"] * origins + c["per_family"] * len(families))
        caveats = ["Counts describe collected and deduplicated events, not the world: coverage gaps change them."]
        penalty = 0
        if new_share > self.s["coverage_share"]:
            penalty = c["coverage_penalty"]
            caveats.append(f"{round(100 * new_share)} % of the supporting origins come from sources first collected this week (coverage change).")
        if self.activity.history_weeks < self.s["min_history_weeks"]:
            caveats.append("Short collected history: baseline may not be representative.")
        if self.data_origin == "fixture":
            caveats.append("Fixture data: fictitious events used to exercise the pipeline.")
        comps = [
            {"key": "statistical", "label": "Statistical strength", "value": round(stat, 1),
             "note": f"{c['per_z']} × |z| = {abs(z):.1f}, max {c['z_max']}" if statistical and z is not None else "not a statistical signal"},
            {"key": "support", "label": "Support", "value": support, "note": f"{len(event_ids)} distinct event(s) × {c['per_event']}"},
            {"key": "corroboration", "label": "Independent corroboration", "value": corr,
             "note": f"{origins} origin(s), families {', '.join(families) or '—'}"},
            {"key": "coverage", "label": "Coverage change", "value": -penalty, "note": "penalty applied" if penalty else "no coverage change"},
        ]
        score = int(max(0, min(100, round(stat + support + corr - penalty))))
        return score, comps, caveats, origins, families

    def _make(self, kind: str, subject: tuple[str, str], statement: str, st, event_ids: list[str], statistical: bool) -> SignalRec:
        score, comps, caveats, origins, families = self._confidence(z=st.z if st else None, event_ids=event_ids, statistical=statistical)
        return SignalRec(
            signal_id=_sid(kind, subject[0], subject[1], iso(self.as_of)),
            kind=kind, subject_type=subject[0], subject_id=subject[1],
            subject_label=self.labels.get(subject[1], subject[1]), statement=statement,
            baseline=st.baseline_mean if st else None, current=st.current if st else len(event_ids),
            deviation=round(st.current - st.baseline_mean, 2) if st else None,
            z_score=st.z if st else None, growth_rate=st.growth if st else None,
            rolling_average=st.rolling_4w if st else None, activity_acceleration=st.acceleration if st else None,
            p_value=(st.p_low if kind == "drop" else st.p_high) if st else None,
            series=st.series if st else [], evidence=event_ids, evidence_count=len(event_ids),
            independent_origins=origins, families=families, confidence_score=score,
            confidence_level=_level(score, self.c), confidence_components=comps, caveats=caveats,
            window_start=iso(self.window_start), window_end=iso(self.as_of),
        )

    # ------------------------------------------------------------------ detection
    def statistical(self) -> list[SignalRec]:
        s = self.s
        out = []
        if self.activity.history_weeks < s["min_history_weeks"]:
            return out
        for subject in sorted(self.activity.counts):
            st = self.activity.stats(subject)
            label = self.labels.get(subject[1], subject[1])
            current_ids = sorted(self.activity.current_events.get(subject, []))
            if st.baseline_mean <= s["surge_max_baseline"] and st.current >= s["surge_min_current"]:
                out.append(self._make("surge", subject,
                    f"{label}: {st.current} distinct events in the last 7 days after a 12-week baseline of "
                    f"{st.baseline_mean:.1f}/week (appearance).", st, current_ids, True))
            elif (st.z >= s["acceleration_z"] and st.current >= s["min_support"]
                  and st.growth is not None and st.growth >= s["acceleration_growth"]):
                out.append(self._make("acceleration", subject,
                    f"{label}: {st.current} distinct events in the last 7 days vs {st.baseline_mean:.1f}/week over the "
                    f"previous 12 weeks (z = {st.z:.1f}, {st.growth:+.0%}).", st, current_ids, True))
            elif (st.baseline_mean >= s["drop_min_baseline"] and st.current <= s["drop_max_ratio"] * st.baseline_mean
                  and st.p_low <= s["drop_max_p"]):
                recent = [e for w, e in self.activity.events_by_subject.get(subject, []) if w >= self.activity.weeks - 5][-6:]
                out.append(self._make("drop", subject,
                    f"{label}: {st.current} event(s) in the last 7 days vs {st.baseline_mean:.1f}/week before "
                    f"(P(X ≤ {st.current}) = {st.p_low:.3f} under the baseline).", st, recent, True))
        return out

    def new_entities(self, entity_types: dict[str, str]) -> list[SignalRec]:
        out = []
        if self.activity.history_weeks < self.s["min_history_weeks"]:
            return out
        start = iso(self.window_start)
        for eid, first in sorted(self.activity.first_seen.items()):
            if entity_types.get(eid) not in ENTITY_TYPES or first < start:
                continue
            subject = (entity_types[eid], eid)
            ids = sorted(self.activity.current_events.get(subject, []))
            if len(ids) < self.s["new_entity_min_events"]:
                continue
            if self._corroboration(ids)[0] < self.s["new_entity_min_origins"]:
                continue                      # a single mention is not yet an observable appearance
            out.append(self._make("new_entity", subject,
                f"{self.labels.get(eid, eid)} appears in collected events for the first time in "
                f"{self.activity.weeks} weeks ({len(ids)} event(s) this week).", None, ids, False))
        return out

    def new_relations(self, relations: list[dict], entity_types: dict[str, str]) -> list[SignalRec]:
        out = []
        if self.activity.history_weeks < self.s["min_history_weeks"]:
            return out
        start = iso(self.window_start)
        new_entities = {e for e, f in self.activity.first_seen.items() if f >= start}
        seen_pairs = set()
        # Explicit (pattern) relations first, so a pair is reported once, with its strongest evidence.
        for r in sorted(relations, key=lambda r: (r["method"] != "pattern", r["relation_id"])):
            a, b = r["source_entity"], r["target_entity"]
            if r["first_seen"] < start or entity_types.get(a) not in ACTOR_TYPES or entity_types.get(b) not in ACTOR_TYPES:
                continue
            if a in new_entities or b in new_entities:
                continue                      # already covered by new_entity
            if tuple(sorted((a, b))) in seen_pairs:
                continue
            seen_pairs.add(tuple(sorted((a, b))))
            confirmed = any(self.events[e].status == "confirmed" for e in r["events"] if e in self.events)
            if r["method"] != "pattern" and not confirmed:
                continue
            ids = [e for e in r["events"] if e in self.events]
            if len(ids) < self.s["new_relation_min_events"]:
                continue
            verb = r["relation_type"].replace("_", " ")
            out.append(self._make("new_relation", ("relation", r["relation_id"]),
                f"{self.labels.get(a, a)} → {self.labels.get(b, b)}: first link in {self.activity.weeks} weeks "
                f"({verb}, {r['method']}, {len(ids)} event(s)).", None, ids, False))
            out[-1].subject_label = f"{self.labels.get(a, a)} → {self.labels.get(b, b)}"
        return out
