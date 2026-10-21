"""Read side of the pipeline database, shared by the JSON API and the site pages.

Every list is filtered and paginated in SQL (never the whole table). Every row
comes back with its scores AND their explanation, its provenance and the
versions that produced it, so a client never needs hidden logic to explain it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.pipeline.store import connect

MAX_PAGE_SIZE = 100
NOTICE_FIXTURE = "FIXTURE DATA — every item is fictitious (replayed test feeds). Do not use as information."
FAMILY_LABEL = {"INS": "institutional", "IND": "industrial", "PRS": "specialised press", "AGG": "aggregator",
                "VID": "official video", "RES": "research"}


class DatabaseMissing(RuntimeError):
    pass


@dataclass
class Page:
    page: int
    page_size: int

    @classmethod
    def of(cls, page: int | None, page_size: int | None) -> "Page":
        return cls(max(1, page or 1), max(1, min(MAX_PAGE_SIZE, page_size or 25)))

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def _j(v, default):
    if v is None:
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


class Reader:
    def __init__(self, path: Path):
        if not Path(path).exists():
            raise DatabaseMissing(f"Pipeline database not found at {path}. Run: python -m app.pipeline run --fixtures --full-rebuild")
        self.con = connect(path, readonly=True)
        self._names: dict[str, str] | None = None
        self._sources: dict[str, sqlite3.Row] | None = None
        self._has_briefs = bool(self.con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'event_briefs'").fetchone())

    def brief(self, event_id: str) -> dict | None:
        """Latest French brief written for this event (language model), or None."""
        if not self._has_briefs:
            return None
        r = self.con.execute("SELECT * FROM event_briefs WHERE event_id = ? ORDER BY created_at DESC LIMIT 1",
                             (event_id,)).fetchone()
        if r is None:
            return None
        return {**_j(r["brief"], {}), "provider": r["provider"], "model": r["model"], "created_at": r["created_at"],
                "origins": r["origins"], "article_pages": r["article_pages"], "prompt_version": r["prompt_version"]}

    def close(self) -> None:
        self.con.close()

    # ------------------------------------------------------------------ helpers
    @property
    def names(self) -> dict[str, str]:
        if self._names is None:
            self._names = {r["entity_id"]: r["canonical_name"] for r in self.con.execute("SELECT entity_id, canonical_name FROM entities")}
        return self._names

    @property
    def sources(self) -> dict[str, sqlite3.Row]:
        if self._sources is None:
            self._sources = {r["source_id"]: r for r in self.con.execute("SELECT * FROM sources")}
        return self._sources

    def last_run(self) -> sqlite3.Row | None:
        return self.con.execute("SELECT * FROM pipeline_runs WHERE status != 'running' ORDER BY started_at DESC LIMIT 1").fetchone()

    def envelope(self) -> dict:
        run = self.last_run()
        origin = run["data_origin"] if run else "unknown"
        out = {"data_origin": origin, "as_of": run["as_of"] if run else None,
               "versions": {"processing": run["processing_version"], "rules": run["rules_version"], "model": run["model_version"]} if run else {}}
        if origin == "fixture":
            out["notice"] = NOTICE_FIXTURE
        return out

    def _ents(self, ids: list[str]) -> list[dict]:
        return [{"id": i, "name": self.names.get(i, i)} for i in ids]

    def _paged(self, base_sql: str, params: list, order: str, page: Page, mapper) -> dict:
        total = self.con.execute(f"SELECT COUNT(*) FROM ({base_sql})", params).fetchone()[0]
        rows = self.con.execute(f"{base_sql} ORDER BY {order} LIMIT ? OFFSET ?", [*params, page.page_size, page.offset]).fetchall()
        return {**self.envelope(), "page": page.page, "page_size": page.page_size, "total": total,
                "pages": (total + page.page_size - 1) // page.page_size, "data": [mapper(r) for r in rows]}

    @staticmethod
    def _json_has(column: str) -> str:
        return f"EXISTS (SELECT 1 FROM json_each({column}) WHERE value = ?)"

    # ------------------------------------------------------------------ items
    def items(self, *, since=None, until=None, sector=None, topic=None, source=None, company=None, country=None,
              technology=None, programme=None, event_type=None, language=None, min_importance=None,
              min_credibility=None, min_novelty=None, status="processed", include_duplicates=False, q=None,
              sort="published", page: Page) -> dict:
        where, p = ["1=1"], []
        if status != "all":
            where.append("processing_status = ?"); p.append(status)
        if not include_duplicates:
            where.append("is_origin_head = 1")
        if since:
            where.append("published_at >= ?"); p.append(since)
        if until:
            where.append("published_at < ?"); p.append(until)
        if sector:
            where.append(self._json_has("sectors")); p.append(sector.upper())
        if topic:
            where.append(f"({self._json_has('topics')} OR {self._json_has('sectors')})"); p += [topic.upper(), topic.upper()]
        if source:
            where.append("source_id = ?"); p.append(source)
        if country:
            where.append(self._json_has("countries")); p.append(country.upper())
        for value in (company, technology, programme):
            if value:
                where.append("id IN (SELECT item_id FROM item_entities WHERE entity_id = ?)"); p.append(value)
        if event_type:
            where.append("event_type = ?"); p.append(event_type)
        if language:
            where.append("language = ?"); p.append(language)
        for col, v in (("importance_score", min_importance), ("credibility_score", min_credibility), ("novelty_score", min_novelty)):
            if v is not None:
                where.append(f"{col} >= ?"); p.append(int(v))
        if q:
            where.append("(title LIKE ? OR summary LIKE ?)"); p += [f"%{q}%", f"%{q}%"]
        order = {"published": "published_at DESC, id", "importance": "importance_score DESC, published_at DESC",
                 "credibility": "credibility_score DESC, published_at DESC", "novelty": "novelty_score DESC, published_at DESC",
                 "quality": "data_quality_score DESC, published_at DESC"}.get(sort, "published_at DESC, id")
        sql = f"SELECT * FROM items WHERE {' AND '.join(where)}"
        return self._paged(sql, p, order, page, self._item)

    def _item(self, r: sqlite3.Row, detail: bool = False) -> dict:
        src = self.sources.get(r["source_id"])
        expl = _j(r["explanation"], {})
        out = {
            "id": r["id"], "title": r["title"], "summary": r["summary"], "url": r["url"], "canonical_url": r["canonical_url"],
            "url_flags": _j(r["url_flags"], []),
            "source": {"id": r["source_id"], "name": src["source_name"] if src else r["source_id"], "type": r["source_type"],
                       "family": r["family"], "family_label": FAMILY_LABEL.get(r["family"], r["family"]), "tier": r["tier"],
                       "publisher": r["publisher"], "endpoint": r["source_url"]},
            "author": r["author"], "language": r["language"], "published_at": r["published_at"], "collected_at": r["collected_at"],
            "sectors": _j(r["sectors"], []), "topics": _j(r["topics"], []), "event_type": r["event_type"],
            "countries": _j(r["countries"], []), "companies": self._ents(_j(r["companies"], [])),
            "organizations": self._ents(_j(r["organizations"], [])), "technologies": self._ents(_j(r["technologies"], [])),
            "programmes": self._ents(_j(r["programmes"], [])),
            "event_id": r["event_id"] or None,
            "dedup": {"duplicate_group_id": r["duplicate_group_id"], "is_origin_head": bool(r["is_origin_head"]),
                      "duplicate_of": r["duplicate_of"] or None, "level": r["dedup_level"] or None,
                      "similarity": r["dedup_similarity"]},
            "scores": {"importance": r["importance_score"], "credibility": r["credibility_score"], "novelty": r["novelty_score"],
                       "strategic": r["strategic_score"], "geopolitical": r["geopolitical_score"],
                       "industrial": r["industrial_score"], "data_quality": r["data_quality_score"]},
            "epistemic_status": "FACT" if r["processing_status"] == "processed" else None,
            "flags": _j(r["flags"], []), "processing_status": r["processing_status"],
            "versions": {"processing": r["processing_version"], "rules": r["rules_version"], "model": r["model_version"],
                         "processed_at": r["processed_at"]},
            "data_origin": r["data_origin"],
            "explanation": expl,
        }
        if detail:
            raw = self.con.execute("SELECT * FROM raw_items WHERE raw_id = ?", (r["raw_id"],)).fetchone()
            out.update({
                "cleaned_content": r["cleaned_content"],
                "raw": {"raw_id": r["raw_id"], "title": raw["title"], "summary": raw["summary"], "raw_content": raw["raw_content"],
                        "published_raw": raw["published_raw"], "language": raw["language"], "fetched_at": raw["fetched_at"],
                        "endpoint": raw["endpoint"], "connector_extra": _j(raw["extra"], {})} if raw else None,
                "mentions": _j(r["mentions"], []), "classification": _j(r["classification"], {}),
                "event_type_evidence": _j(r["event_type_evidence"], []), "numeric_facts": _j(r["numeric_facts"], []),
                "logs": [dict(x) for x in self.con.execute(
                    "SELECT run_id, stage, level, code, message FROM processing_logs WHERE item_id = ? ORDER BY log_id DESC LIMIT 20", (r["raw_id"],))],
            })
        return out

    def item(self, item_id: str) -> dict | None:
        r = self.con.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return {**self.envelope(), **self._item(r, detail=True)} if r else None

    # ------------------------------------------------------------------ events
    def events(self, *, since=None, until=None, sector=None, topic=None, source=None, company=None, country=None,
               technology=None, programme=None, status=None, confidence=None, event_type=None, min_importance=None,
               min_credibility=None, min_novelty=None, q=None, sort="importance", page: Page, entity=None) -> dict:
        where, p = ["1=1"], []
        if entity:
            # Same rule as entities.event_count: an article of the event mentions the entity (name or alias).
            where.append(f"event_id IN ({self.ENTITY_EVENTS})"); p.append(entity)
        if since:
            where.append("first_seen >= ?"); p.append(since)
        if until:
            where.append("first_seen < ?"); p.append(until)
        if sector:
            where.append(self._json_has("sectors")); p.append(sector.upper())
        if topic:
            where.append(f"({self._json_has('topics')} OR {self._json_has('sectors')})"); p += [topic.upper(), topic.upper()]
        if source:
            where.append(self._json_has("source_ids")); p.append(source)
        if country:
            where.append(self._json_has("countries")); p.append(country.upper())
        if company:
            where.append(f"({self._json_has('companies')} OR {self._json_has('organizations')})"); p += [company, company]
        if technology:
            where.append(self._json_has("technologies")); p.append(technology)
        if programme:
            where.append(self._json_has("programmes")); p.append(programme)
        if status:
            where.append("status = ?"); p.append(status)
        if confidence:
            where.append("confidence_level = ?"); p.append(confidence)
        if event_type:
            where.append("event_type = ?"); p.append(event_type)
        for col, v in (("importance", min_importance), ("credibility", min_credibility), ("novelty", min_novelty)):
            if v is not None:
                where.append(f"{col} >= ?"); p.append(int(v))
        if q:
            where.append("(title LIKE ? OR description LIKE ?)"); p += [f"%{q}%", f"%{q}%"]
        order = {"importance": "importance DESC, first_seen DESC", "recent": "first_seen DESC",
                 "credibility": "credibility DESC, first_seen DESC", "novelty": "novelty DESC, first_seen DESC",
                 "strategic": "strategic DESC, first_seen DESC"}.get(sort, "importance DESC, first_seen DESC")
        return self._paged(f"SELECT * FROM events WHERE {' AND '.join(where)}", p, order, page, self._event)

    ENTITY_EVENTS = ("SELECT DISTINCT i.event_id FROM item_entities ie JOIN items i ON i.id = ie.item_id "
                     "WHERE ie.entity_id = ? AND i.event_id != ''")

    def entity_event_dates(self, entity_ids: list[str] | None = None) -> dict[str, list[str]]:
        """first_seen of every existing event per entity (for activity charts), in one query."""
        sql = ("SELECT ie.entity_id AS eid, ev.event_id, ev.first_seen FROM item_entities ie JOIN items i ON i.id = ie.item_id "
               "JOIN events ev ON ev.event_id = i.event_id")
        params: list = []
        if entity_ids:
            sql += f" WHERE ie.entity_id IN ({','.join('?' * len(entity_ids))})"
            params = list(entity_ids)
        out: dict[str, dict[str, str]] = {}
        for r in self.con.execute(sql + " GROUP BY ie.entity_id, ev.event_id", params):
            out.setdefault(r["eid"], {})[r["event_id"]] = r["first_seen"]
        return {k: list(v.values()) for k, v in out.items()}

    def label_event_dates(self) -> dict[str, list[str]]:
        """first_seen of every event per taxonomy label (sectors and topics)."""
        out: dict[str, list[str]] = {}
        for col in ("sectors", "topics"):
            for r in self.con.execute(f"SELECT value AS label, first_seen FROM events, json_each(events.{col})"):
                out.setdefault(r["label"], []).append(r["first_seen"])
        return out

    def _origins(self, ev: sqlite3.Row) -> list[dict]:
        rows = self.con.execute(
            "SELECT i.* FROM event_items ei JOIN items i ON i.id = ei.item_id WHERE ei.event_id = ? ORDER BY i.published_at",
            (ev["event_id"],)).fetchall()
        groups: dict[str, dict] = {}
        for r in rows:
            g = groups.setdefault(r["duplicate_group_id"], {"origin_id": r["duplicate_group_id"], "head": None, "copies": []})
            src = self.sources.get(r["source_id"])
            entry = {"item_id": r["id"], "title": r["title"], "url": r["url"], "published_at": r["published_at"],
                     "source": src["source_name"] if src else r["source_id"], "source_id": r["source_id"],
                     "family": r["family"], "tier": r["tier"], "publisher": r["publisher"],
                     "primary": bool(src and src["publisher_entity"] and src["publisher_entity"] in
                                     set(_j(ev["companies"], []) + _j(ev["organizations"], [])))}
            if r["is_origin_head"]:
                g["head"] = entry
            else:
                g["copies"].append({**entry, "dedup_level": r["dedup_level"], "duplicate_of": r["duplicate_of"],
                                    "similarity": r["dedup_similarity"]})
        return list(groups.values())

    def _why(self, ev: sqlite3.Row, expl: dict, origins: list[dict] | None = None) -> dict:
        def top(metric: str, n: int = 3) -> list[str]:
            comps = expl.get(metric, {}).get("components", [])
            key = "contribution" if comps and "contribution" in comps[0] else "value"
            ranked = sorted([c for c in comps if c.get(key) and c[key] > 0], key=lambda c: -c[key])[:n]
            ranked += [c for c in comps if c.get(key) and c[key] < 0]      # a penalty is always shown
            return [f"{c['label']}: {c[key]:+g} — {c['note']}" for c in ranked]

        orig = len(_j(ev["origin_ids"], []))
        fams = _j(ev["families"], [])
        contra = _j(ev["contradictions"], [])
        if ev["status"] == "contested":
            epi = "FACT, CONTESTED — " + "; ".join(
                f"{c['fact']}: " + " vs ".join(f"{v['value']:g} ({', '.join(v['sources'])})" for v in c["values"]) for c in contra)
        elif ev["status"] == "confirmed":
            epi = f"FACT, confirmed — reported by {orig} independent origins from {len(fams)} source families."
        else:
            epi = "FACT, candidate — a single origin so far; not corroborated."
        why = {"important": top("importance"), "new": top("novelty"), "credible": top("credibility"),
               "strategic": top("strategic", 5), "epistemic_status": epi}
        if origins is not None:
            why["corroborated_by"] = [f"{o['head']['source']} ({FAMILY_LABEL.get(o['head']['family'])}, tier {o['head']['tier']})"
                                      + (" — primary source" if o["head"]["primary"] else "")
                                      + (f" + {len(o['copies'])} copy/copies" if o["copies"] else "")
                                      for o in origins if o["head"]]
        return why

    def _event(self, r: sqlite3.Row, detail: bool = False) -> dict:
        expl = _j(r["explanation"], {})
        items, origins, fams = _j(r["item_ids"], []), _j(r["origin_ids"], []), _j(r["families"], [])
        out = {
            "id": r["event_id"], "title": r["title"], "description": r["description"], "event_type": r["event_type"],
            "event_date": r["event_date"], "first_seen": r["first_seen"], "last_seen": r["last_seen"],
            "status": r["status"], "epistemic_status": r["epistemic_status"], "confidence_level": r["confidence_level"],
            "proof": {"src": len(items), "orig": len(origins), "fam": len(fams), "families": fams},
            "primary_source": bool(r["primary_source"]), "sources": _j(r["source_ids"], []),
            "companies": self._ents(_j(r["companies"], [])), "organizations": self._ents(_j(r["organizations"], [])),
            "technologies": self._ents(_j(r["technologies"], [])), "programmes": self._ents(_j(r["programmes"], [])),
            "countries": _j(r["countries"], []), "sectors": _j(r["sectors"], []), "topics": _j(r["topics"], []),
            "scores": {"importance": r["importance"], "credibility": r["credibility"], "confidence": r["credibility"],
                       "novelty": r["novelty"], "strategic": r["strategic"], "geopolitical": r["geopolitical"],
                       "industrial": r["industrial"], "relevance": r["relevance"]},
            "contradictions": _j(r["contradictions"], []),
            "versions": {"processing": r["processing_version"], "rules": r["rules_version"], "model": r["model_version"],
                         "processed_at": r["processed_at"], "run_id": r["run_id"]},
            "data_origin": r["data_origin"],
            "brief": self.brief(r["event_id"]),
        }
        if detail:
            org = self._origins(r)
            out["why"] = self._why(r, expl, org)
            out["origins"] = org
            out["confidence_rules"] = _j(r["confidence_rules"], [])
            out["explanation"] = expl
            out["signals"] = [{"id": s["signal_id"], "kind": s["kind"], "statement": s["statement"], "confidence_level": s["confidence_level"]}
                              for s in self.con.execute("SELECT * FROM signals WHERE window_end = (SELECT MAX(window_end) FROM signals) AND "
                                                        + self._json_has("evidence"), (r["event_id"],))]
        else:
            out["why"] = self._why(r, expl)
        return out

    def event(self, event_id: str) -> dict | None:
        r = self.con.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return {**self.envelope(), **self._event(r, detail=True)} if r else None

    # ------------------------------------------------------------------ signals
    def signals(self, *, kind=None, subject_type=None, subject=None, min_confidence=None, level=None, window_end=None,
                page: Page) -> dict:
        where, p = [], []
        if window_end:
            where.append("window_end = ?"); p.append(window_end)
        else:
            where.append("window_end = (SELECT MAX(window_end) FROM signals)")
        if kind:
            where.append("kind = ?"); p.append(kind)
        if subject_type:
            where.append("subject_type = ?"); p.append(subject_type)
        if subject:
            where.append("subject_id = ?"); p.append(subject)
        if min_confidence is not None:
            where.append("confidence_score >= ?"); p.append(int(min_confidence))
        if level:
            where.append("confidence_level = ?"); p.append(level)
        return self._paged(f"SELECT * FROM signals WHERE {' AND '.join(where)}", p,
                           "confidence_score DESC, evidence_count DESC, signal_id", page, self._signal)

    def _signal(self, r: sqlite3.Row) -> dict:
        evidence = _j(r["evidence"], [])
        titles = {}
        if evidence:
            q = ",".join("?" * len(evidence))
            titles = {x["event_id"]: (x["title"], x["status"]) for x in
                      self.con.execute(f"SELECT event_id, title, status FROM events WHERE event_id IN ({q})", evidence)}
        return {
            "id": r["signal_id"], "kind": r["kind"], "subject": {"type": r["subject_type"], "id": r["subject_id"], "label": r["subject_label"]},
            "statement": r["statement"], "epistemic_status": r["epistemic_status"],
            "epistemic_note": "Observation on collected, deduplicated events. Not a forecast.",
            "stats": {"baseline_weekly_mean": r["baseline"], "current_week": r["current"], "deviation": r["deviation"],
                      "z_score": r["z_score"], "growth_rate": r["growth_rate"], "rolling_average_4w": r["rolling_average"],
                      "activity_acceleration": r["activity_acceleration"], "p_value": r["p_value"],
                      "weekly_series": _j(r["series"], [])},
            "confidence": {"score": r["confidence_score"], "level": r["confidence_level"],
                           "components": _j(r["confidence_components"], [])},
            "evidence_count": r["evidence_count"], "independent_origins": r["independent_origins"], "families": _j(r["families"], []),
            "evidence": [{"event_id": e, "title": titles.get(e, ("", ""))[0], "status": titles.get(e, ("", ""))[1]} for e in evidence],
            "caveats": _j(r["caveats"], []), "window": {"start": r["window_start"], "end": r["window_end"]},
            "versions": {"processing": r["processing_version"], "rules": r["rules_version"], "run_id": r["run_id"]},
            "data_origin": r["data_origin"],
        }

    # ------------------------------------------------------------------ entities
    def entities(self, *, q=None, type=None, country=None, sector=None, min_events=None, page: Page) -> dict:
        where, p = ["1=1"], []
        if q:
            where.append("(canonical_name LIKE ? OR aliases LIKE ? OR entity_id = ?)"); p += [f"%{q}%", f"%{q}%", q]
        if type:
            where.append("entity_type = ?"); p.append(type)
        if country:
            where.append("country = ?"); p.append(country.upper())
        if sector:
            where.append("sector = ?"); p.append(sector.upper())
        if min_events is not None:
            where.append("event_count >= ?"); p.append(int(min_events))
        return self._paged(f"SELECT * FROM entities WHERE {' AND '.join(where)}", p,
                           "event_count DESC, canonical_name", page, self._entity)

    def _entity(self, r: sqlite3.Row) -> dict:
        return {"id": r["entity_id"], "name": r["canonical_name"], "type": r["entity_type"], "aliases": _j(r["aliases"], []),
                "country": r["country"], "sector": r["sector"], "parent": r["parent_entity"] or None,
                "item_count": r["item_count"], "event_count": r["event_count"], "first_seen": r["first_seen"],
                "last_seen": r["last_seen"], "origin": r["origin"]}

    def entity(self, entity_id: str) -> dict | None:
        r = self.con.execute("SELECT * FROM entities WHERE entity_id = ?", (entity_id,)).fetchone()
        if not r:
            return None
        rels = [{"id": x["relation_id"], "source": {"id": x["source_entity"], "name": self.names.get(x["source_entity"])},
                 "target": {"id": x["target_entity"], "name": self.names.get(x["target_entity"])},
                 "type": x["relation_type"], "method": x["method"], "weight": x["weight"], "confidence": x["confidence"],
                 "first_seen": x["first_seen"], "last_seen": x["last_seen"], "evidence": _j(x["evidence"], []),
                 "events": _j(x["events"], [])[-5:]}
                for x in self.con.execute("SELECT * FROM entity_relations WHERE source_entity = ? OR target_entity = ? "
                                          "ORDER BY weight DESC, last_seen DESC LIMIT 30", (entity_id, entity_id))]
        events = [{"id": e["event_id"], "title": e["title"], "first_seen": e["first_seen"], "status": e["status"], "importance": e["importance"]}
                  for e in self.con.execute(
                      "SELECT event_id, title, first_seen, status, importance FROM events WHERE "
                      f"{self._json_has('companies')} OR {self._json_has('organizations')} OR {self._json_has('technologies')} "
                      f"OR {self._json_has('programmes')} ORDER BY first_seen DESC LIMIT 10", (entity_id,) * 4)]
        signals = [{"id": s["signal_id"], "kind": s["kind"], "statement": s["statement"], "confidence_level": s["confidence_level"]}
                   for s in self.con.execute("SELECT * FROM signals WHERE subject_id = ? AND window_end = (SELECT MAX(window_end) FROM signals)",
                                             (entity_id,))]
        return {**self.envelope(), **self._entity(r), "relations": rels, "recent_events": events, "signals": signals,
                "relation_note": "cooccurrence = named in the same event (weak); pattern = explicit verb in a sentence."}

    # ------------------------------------------------------------------ sources, topics, metrics, runs
    def source_list(self) -> dict:
        counts = {r["source_id"]: (r["n"], r["heads"]) for r in self.con.execute(
            "SELECT source_id, COUNT(*) AS n, SUM(is_origin_head) AS heads FROM items GROUP BY source_id")}
        data = []
        for s in sorted(self.sources.values(), key=lambda s: (s["source_tier"], s["source_id"])):
            n, heads = counts.get(s["source_id"], (0, 0))
            data.append({"id": s["source_id"], "name": s["source_name"], "type": s["source_type"], "family": s["family"],
                         "connector": s["connector"], "tier": s["source_tier"], "baseline_credibility": s["baseline_credibility"],
                         "domain": s["domain"], "country": s["country"], "institution": s["institution"],
                         "publisher_entity": s["publisher_entity"] or None, "enabled": bool(s["enabled"]),
                         "status": s["last_status"] or "never_run", "last_error": s["last_error"],
                         "last_attempt_at": s["last_attempt_at"], "last_success_at": s["last_success_at"],
                         "items_last_run": s["items_last_run"], "new_items_last_run": s["new_items_last_run"],
                         "collect_seconds": s["collect_seconds"], "items_stored": n, "origins_stored": heads or 0,
                         "endpoint": s["endpoint"]})
        return {**self.envelope(), "total": len(data), "data": data,
                "tier_note": "Tier 1 institutional/primary · 2 specialised press · 3 aggregators · 4 low reliability. "
                             "A tier is one input of credibility, never a verdict."}

    def topics(self) -> dict:
        run = self.last_run()
        as_of = datetime.fromisoformat(run["as_of"]) if run else None
        rows = []
        if as_of:
            start = (as_of - timedelta(days=7)).isoformat()
            base_start = (as_of - timedelta(days=91)).isoformat()
            q = ("SELECT value AS label, SUM(first_seen >= ?) AS current, SUM(first_seen < ? AND first_seen >= ?) AS base "
                 "FROM events, json_each(events.{col}) GROUP BY value")
            agg: dict[str, list[int]] = {}
            for col in ("sectors", "topics"):
                for r in self.con.execute(q.format(col=col), (start, start, base_start)):
                    agg[r["label"]] = [r["current"] or 0, r["base"] or 0, col]
            signals = {s["subject_id"]: s["kind"] for s in self.con.execute(
                "SELECT subject_id, kind FROM signals WHERE subject_type = 'topic' AND window_end = (SELECT MAX(window_end) FROM signals)")}
            for label, (cur, base, col) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
                rows.append({"label": label, "kind": "sector" if col == "sectors" else "topic", "events_last_7d": cur,
                             "baseline_weekly_mean": round(base / 12, 2), "signal": signals.get(label)})
        return {**self.envelope(), "total": len(rows), "data": rows,
                "note": "Counts of distinct events (not articles) per taxonomy label."}

    def metrics(self) -> dict:
        c = self.con
        run = self.last_run()
        count = lambda sql, *a: c.execute(sql, a).fetchone()[0]  # noqa: E731
        return {**self.envelope(),
                "tables": {t: count(f"SELECT COUNT(*) FROM {t}") for t in
                           ("sources", "raw_items", "items", "events", "entities", "entity_relations", "signals", "pipeline_runs", "processing_logs")},
                "items": {"processed": count("SELECT COUNT(*) FROM items WHERE processing_status='processed'"),
                          "out_of_scope": count("SELECT COUNT(*) FROM items WHERE processing_status='out_of_scope'"),
                          "origins": count("SELECT COUNT(*) FROM items WHERE is_origin_head=1"),
                          "duplicates": count("SELECT COUNT(*) FROM items WHERE is_origin_head=0"),
                          "dedup_levels": {r[0]: r[1] for r in c.execute(
                              "SELECT dedup_level, COUNT(*) FROM items WHERE dedup_level != '' GROUP BY dedup_level")},
                          "date_estimated": count("SELECT COUNT(*) FROM items WHERE flags LIKE '%date_estimated%'"),
                          "mean_data_quality": count("SELECT ROUND(AVG(data_quality_score),1) FROM items")},
                "events": {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM events GROUP BY status")},
                "signals": {r[0]: r[1] for r in c.execute(
                    "SELECT kind, COUNT(*) FROM signals WHERE window_end = (SELECT MAX(window_end) FROM signals) GROUP BY kind")},
                "entities_seen": count("SELECT COUNT(*) FROM entities WHERE item_count > 0"),
                "last_run": self._run(run) if run else None}

    def _run(self, r: sqlite3.Row) -> dict:
        return {"run_id": r["run_id"], "status": r["status"], "mode": r["mode"], "data_origin": r["data_origin"], "as_of": r["as_of"],
                "started_at": r["started_at"], "finished_at": r["finished_at"], "total_seconds": r["total_seconds"],
                "versions": {"processing": r["processing_version"], "rules": r["rules_version"], "model": r["model_version"],
                             "sources_file": r["sources_file"]},
                "args": _j(r["args"], {}), "metrics": _j(r["metrics"], {}), "stages": _j(r["stages"], []),
                "sources": {"ok": r["sources_ok"], "warning": r["sources_warning"], "error": r["sources_error"]},
                "error_summary": r["error_summary"]}

    def runs(self, page: Page) -> dict:
        return self._paged("SELECT * FROM pipeline_runs", [], "started_at DESC", page, self._run)

    def run(self, run_id: str, level: str | None, page: Page) -> dict | None:
        r = self.con.execute("SELECT * FROM pipeline_runs WHERE run_id = ?", (run_id,)).fetchone()
        if not r:
            return None
        where, p = ["run_id = ?"], [run_id]
        if level:
            where.append("level = ?"); p.append(level)
        logs = self._paged(f"SELECT log_id, ts, stage, level, code, source_id, item_id, message FROM processing_logs WHERE {' AND '.join(where)}",
                           p, "log_id", page, dict)
        return {**self.envelope(), **self._run(r), "logs": {k: logs[k] for k in ("page", "page_size", "total", "pages", "data")}}

    def status(self) -> dict:
        run = self.last_run()
        src = self.source_list()["data"]
        errors = [s for s in src if s["status"] == "error"]
        warnings = [s for s in src if s["status"] == "warning"]
        last_err = self.con.execute("SELECT ts, stage, code, source_id, message FROM processing_logs WHERE level = 'error' "
                                    "ORDER BY log_id DESC LIMIT 1").fetchone()
        count = lambda sql: self.con.execute(sql).fetchone()[0]  # noqa: E731
        return {**self.envelope(),
                "last_run": self._run(run) if run else None,
                "sources": {"configured": len(src), "active": sum(s["enabled"] for s in src), "ok": sum(s["status"] == "ok" for s in src),
                            "warning": len(warnings), "error": len(errors),
                            "in_error": [{"id": s["id"], "name": s["name"], "error": s["last_error"]} for s in errors],
                            "with_warnings": [{"id": s["id"], "name": s["name"], "warning": s["last_error"]} for s in warnings]},
                "counts": {"articles": count("SELECT COUNT(*) FROM items"),
                           "duplicates": count("SELECT COUNT(*) FROM items WHERE is_origin_head = 0"),
                           "events": count("SELECT COUNT(*) FROM events"),
                           "signals": count("SELECT COUNT(*) FROM signals WHERE window_end = (SELECT MAX(window_end) FROM signals)"),
                           "entities_seen": count("SELECT COUNT(*) FROM entities WHERE item_count > 0")},
                "last_error": dict(last_err) if last_err else None}

    # ------------------------------------------------------------------ search suggestions
    def suggest(self, q: str, limit: int = 8) -> list[dict]:
        needle = f"%{q}%"
        rows = self.con.execute("SELECT * FROM entities WHERE canonical_name LIKE ? OR aliases LIKE ? "
                                "ORDER BY (canonical_name LIKE ?) DESC, event_count DESC LIMIT ?", (needle, needle, f"{q}%", limit))
        return [self._entity(r) for r in rows]


def open_reader(path: Path) -> Reader:
    return Reader(path)
