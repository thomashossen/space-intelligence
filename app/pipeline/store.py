"""STORE: SQLite persistence (stdlib sqlite3, one file, WAL).

Tables
  sources           registry + collection health (last status, last error)
  raw_items         entries exactly as delivered (raw_content never modified)
  items             canonical records (one column per Item field, lists as JSON)
  item_entities     item × entity mentions, for filtering
  entities          controlled nomenclature + observed counts
  entity_relations  evidence-backed edges (accumulated across runs)
  events            consolidated events (recomputed over the analysis window)
  event_items       event × item, with the origin (duplicate group) of each item
  signals           signal candidates, one set per as-of time (history kept)
  pipeline_runs     one row per run: args, versions, per-stage metrics
  processing_logs   warnings and errors with stage, source and item
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, fields
from pathlib import Path

from app.pipeline.models import EventRec, Item, SignalRec

SCHEMA_VERSION = 1

_ITEM_FIELDS = [f.name for f in fields(Item)]
_EVENT_FIELDS = [f.name for f in fields(EventRec)]
_SIGNAL_FIELDS = [f.name for f in fields(SignalRec)]
_JSON = (list, dict, tuple)


def _cols(names: list[str], types: dict[str, str], pk: str) -> str:
    return ",\n  ".join(f"{n} {types.get(n, 'TEXT')}{' PRIMARY KEY' if n == pk else ''}" for n in names)


_INT = "INTEGER"
_REAL = "REAL"
ITEM_TYPES = {"tier": _INT, "is_origin_head": _INT, "importance_score": _INT, "credibility_score": _INT,
              "novelty_score": _INT, "strategic_score": _INT, "geopolitical_score": _INT, "industrial_score": _INT,
              "data_quality_score": _INT, "language_confidence": _REAL, "dedup_similarity": _REAL}
EVENT_TYPES = {"primary_source": _INT, "importance": _INT, "credibility": _INT, "novelty": _INT, "strategic": _INT,
               "geopolitical": _INT, "industrial": _INT, "relevance": _INT}
SIGNAL_TYPES = {"baseline": _REAL, "current": _REAL, "deviation": _REAL, "z_score": _REAL, "growth_rate": _REAL,
                "rolling_average": _REAL, "activity_acceleration": _REAL, "p_value": _REAL, "evidence_count": _INT,
                "independent_origins": _INT, "confidence_score": _INT}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY, source_name TEXT, source_type TEXT, family TEXT, connector TEXT,
  domain TEXT, country TEXT, institution TEXT, publisher_entity TEXT, source_tier INTEGER,
  baseline_credibility REAL, language TEXT, endpoint TEXT, enabled INTEGER, data_origin TEXT,
  last_run_id TEXT, last_status TEXT, last_error TEXT, last_attempt_at TEXT, last_success_at TEXT,
  items_last_run INTEGER DEFAULT 0, new_items_last_run INTEGER DEFAULT 0, collect_seconds REAL
);
CREATE TABLE IF NOT EXISTS raw_items (
  raw_id TEXT PRIMARY KEY, source_id TEXT, run_id TEXT, url TEXT, title TEXT, summary TEXT,
  raw_content TEXT, author TEXT, language TEXT, published_raw TEXT, fetched_at TEXT, endpoint TEXT, extra TEXT
);
CREATE TABLE IF NOT EXISTS items (
  {_cols(_ITEM_FIELDS, ITEM_TYPES, 'id')}
);
CREATE INDEX IF NOT EXISTS ix_items_pub ON items(published_at);
CREATE INDEX IF NOT EXISTS ix_items_event ON items(event_id);
CREATE INDEX IF NOT EXISTS ix_items_source ON items(source_id);
CREATE INDEX IF NOT EXISTS ix_items_group ON items(duplicate_group_id);
CREATE TABLE IF NOT EXISTS item_entities (
  item_id TEXT, entity_id TEXT, entity_type TEXT, surface TEXT, count INTEGER, method TEXT,
  PRIMARY KEY (item_id, entity_id)
);
CREATE INDEX IF NOT EXISTS ix_ie_entity ON item_entities(entity_id);
CREATE TABLE IF NOT EXISTS entities (
  entity_id TEXT PRIMARY KEY, canonical_name TEXT, entity_type TEXT, aliases TEXT, country TEXT,
  sector TEXT, parent_entity TEXT, item_count INTEGER DEFAULT 0, event_count INTEGER DEFAULT 0,
  first_seen TEXT, last_seen TEXT, origin TEXT DEFAULT 'gazetteer'
);
CREATE TABLE IF NOT EXISTS entity_relations (
  relation_id TEXT PRIMARY KEY, source_entity TEXT, target_entity TEXT, relation_type TEXT, method TEXT,
  weight INTEGER, confidence TEXT, events TEXT, evidence TEXT, first_seen TEXT, last_seen TEXT, rules_version TEXT
);
CREATE TABLE IF NOT EXISTS events (
  {_cols(_EVENT_FIELDS, EVENT_TYPES, 'event_id')},
  processing_version TEXT, rules_version TEXT, model_version TEXT, processed_at TEXT, run_id TEXT, data_origin TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_first ON events(first_seen);
CREATE TABLE IF NOT EXISTS event_items (
  event_id TEXT, item_id TEXT, origin_id TEXT, is_head INTEGER, PRIMARY KEY (event_id, item_id)
);
CREATE TABLE IF NOT EXISTS signals (
  {_cols(_SIGNAL_FIELDS, SIGNAL_TYPES, 'signal_id')},
  processing_version TEXT, rules_version TEXT, run_id TEXT, data_origin TEXT, detected_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_signals_window ON signals(window_end);
CREATE TABLE IF NOT EXISTS pipeline_runs (
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, mode TEXT, data_origin TEXT,
  as_of TEXT, args TEXT, processing_version TEXT, rules_version TEXT, model_version TEXT,
  sources_file TEXT, metrics TEXT, stages TEXT, sources_ok INTEGER, sources_warning INTEGER,
  sources_error INTEGER, total_seconds REAL, error_summary TEXT
);
CREATE TABLE IF NOT EXISTS processing_logs (
  log_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts TEXT, stage TEXT, level TEXT,
  code TEXT, source_id TEXT, item_id TEXT, message TEXT
);
CREATE INDEX IF NOT EXISTS ix_logs_run ON processing_logs(run_id);
"""

DERIVED_TABLES = ("sources", "raw_items", "items", "item_entities", "entities", "entity_relations",
                  "events", "event_items", "signals")


def _enc(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, _JSON):
        return json.dumps(v, ensure_ascii=False, sort_keys=isinstance(v, dict))
    return v


def _dec(v, default):
    if isinstance(default, bool):
        return bool(v)
    if isinstance(default, (list, dict)) and isinstance(v, str):
        return json.loads(v)
    return v


def connect(path: Path | str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    return con


_ITEM_DEFAULTS = Item(id="", raw_id="", source_id="", source_type="", family="", tier=0, source_url="", url="", canonical_url="")
_EVENT_DEFAULTS = EventRec("", "", "", "", "", "", "", [], [], [], [], [])


def row_to_item(row: sqlite3.Row) -> Item:
    data = {k: _dec(row[k], getattr(_ITEM_DEFAULTS, k)) for k in _ITEM_FIELDS}
    if isinstance(data.get("embedding"), str):
        data["embedding"] = json.loads(data["embedding"])
    return Item(**data)


def row_to_event(row: sqlite3.Row) -> EventRec:
    return EventRec(**{k: _dec(row[k], getattr(_EVENT_DEFAULTS, k)) for k in _EVENT_FIELDS})


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.con = connect(self.path)
        self.con.executescript(SCHEMA)
        self.con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.con.close()

    def reset(self) -> None:
        """Full rebuild: drop derived data, keep the run history and its logs."""
        with self.con:
            for t in DERIVED_TABLES:
                self.con.execute(f"DELETE FROM {t}")

    # ------------------------------------------------------------------ runs and logs
    def start_run(self, run: dict) -> None:
        with self.con:
            self.con.execute("INSERT INTO pipeline_runs (run_id, started_at, status, mode, data_origin, as_of, args, processing_version, "
                             "rules_version, model_version, sources_file) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (run["run_id"], run["started_at"], "running", run["mode"], run["data_origin"], run["as_of"],
                              json.dumps(run["args"]), run["processing_version"], run["rules_version"], run["model_version"],
                              run["sources_file"]))

    def finish_run(self, run_id: str, **values) -> None:
        values = {k: _enc(v) for k, v in values.items()}
        cols = ", ".join(f"{k} = ?" for k in values)
        with self.con:
            self.con.execute(f"UPDATE pipeline_runs SET {cols} WHERE run_id = ?", (*values.values(), run_id))

    def add_logs(self, run_id: str, ts: str, logs: list) -> None:
        with self.con:
            self.con.executemany(
                "INSERT INTO processing_logs (run_id, ts, stage, level, code, source_id, item_id, message) VALUES (?,?,?,?,?,?,?,?)",
                [(run_id, ts, l.stage, l.level, l.code, l.source_id, l.item_id, l.message[:1000]) for l in logs])

    # ------------------------------------------------------------------ sources
    def upsert_source(self, src, baseline: float, data_origin: str, status: dict | None = None) -> None:
        row = {
            "source_id": src.id, "source_name": src.name, "source_type": src.source_type, "family": src.family,
            "connector": src.connector, "domain": src.domain, "country": src.country, "institution": src.institution,
            "publisher_entity": src.publisher_entity, "source_tier": src.tier, "baseline_credibility": baseline,
            "language": src.language, "endpoint": src.url or ", ".join(src.fixture), "enabled": int(src.enabled),
            "data_origin": data_origin,
        }
        row.update(status or {})
        cols = ", ".join(row)
        updates = ", ".join(f"{k} = excluded.{k}" for k in row if k != "source_id")
        with self.con:
            self.con.execute(f"INSERT INTO sources ({cols}) VALUES ({', '.join('?' * len(row))}) "
                             f"ON CONFLICT(source_id) DO UPDATE SET {updates}", tuple(row.values()))

    def source_first_seen(self) -> dict[str, str]:
        rows = self.con.execute("SELECT source_id, MIN(published_at) AS first FROM items GROUP BY source_id")
        return {r["source_id"]: r["first"] for r in rows}

    # ------------------------------------------------------------------ raw items
    def known_raw_ids(self, ids: list[str]) -> set[str]:
        known = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows = self.con.execute(f"SELECT raw_id FROM raw_items WHERE raw_id IN ({','.join('?' * len(chunk))})", chunk)
            known |= {r["raw_id"] for r in rows}
        return known

    def insert_raw(self, rows: list[tuple]) -> None:
        with self.con:
            self.con.executemany("INSERT OR IGNORE INTO raw_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    # ------------------------------------------------------------------ items
    def upsert_items(self, items: list[Item]) -> None:
        cols = ", ".join(_ITEM_FIELDS)
        marks = ", ".join("?" * len(_ITEM_FIELDS))
        with self.con:
            self.con.executemany(f"INSERT OR REPLACE INTO items ({cols}) VALUES ({marks})",
                                 [tuple(_enc(getattr(it, f)) for f in _ITEM_FIELDS) for it in items])

    def replace_item_entities(self, items: list[Item]) -> None:
        with self.con:
            for it in items:
                self.con.execute("DELETE FROM item_entities WHERE item_id = ?", (it.id,))
                self.con.executemany("INSERT OR REPLACE INTO item_entities VALUES (?,?,?,?,?,?)",
                                     [(it.id, m["entity_id"], m["type"], m["surface"], m["count"], m["method"]) for m in it.mentions])

    def items_since(self, since_iso: str) -> list[Item]:
        rows = self.con.execute("SELECT * FROM items WHERE published_at >= ? ORDER BY published_at, id", (since_iso,))
        return [row_to_item(r) for r in rows]

    def count(self, table: str, where: str = "", params: tuple = ()) -> int:
        return self.con.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]

    # ------------------------------------------------------------------ events, relations, signals
    def replace_events(self, since_iso: str, events: list[EventRec], meta: dict) -> None:
        cols = _EVENT_FIELDS + list(meta)
        with self.con:
            old = [r[0] for r in self.con.execute("SELECT event_id FROM events WHERE first_seen >= ?", (since_iso,))]
            for i in range(0, len(old), 500):
                chunk = old[i:i + 500]
                q = ",".join("?" * len(chunk))
                self.con.execute(f"DELETE FROM event_items WHERE event_id IN ({q})", chunk)
                self.con.execute(f"DELETE FROM events WHERE event_id IN ({q})", chunk)
            self.con.executemany(
                f"INSERT OR REPLACE INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [tuple(_enc(getattr(ev, f)) for f in _EVENT_FIELDS) + tuple(meta.values()) for ev in events])

    def replace_event_items(self, rows: list[tuple]) -> None:
        with self.con:
            self.con.executemany("INSERT OR REPLACE INTO event_items VALUES (?,?,?,?)", rows)

    def merge_relations(self, relations: list[dict], rules_version: str) -> None:
        with self.con:
            for r in relations:
                old = self.con.execute("SELECT events, first_seen, last_seen FROM entity_relations WHERE relation_id = ?",
                                       (r["relation_id"],)).fetchone()
                events = r["events"]
                first, last = r["first_seen"], r["last_seen"]
                if old:
                    events = list(dict.fromkeys(json.loads(old["events"]) + events))[-50:]
                    first, last = min(first, old["first_seen"]), max(last, old["last_seen"])
                weight = len(events)
                conf = "moderate" if r["method"] == "pattern" or weight >= 3 else "low"
                self.con.execute(
                    "INSERT OR REPLACE INTO entity_relations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["relation_id"], r["source_entity"], r["target_entity"], r["relation_type"], r["method"], weight,
                     conf, json.dumps(events), json.dumps(r["evidence"], ensure_ascii=False), first, last, rules_version))

    def replace_signals(self, window_end: str, signals: list[SignalRec], meta: dict) -> None:
        cols = _SIGNAL_FIELDS + list(meta)
        with self.con:
            self.con.execute("DELETE FROM signals WHERE window_end = ?", (window_end,))
            self.con.executemany(
                f"INSERT OR REPLACE INTO signals ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [tuple(_enc(getattr(s, f)) for f in _SIGNAL_FIELDS) + tuple(meta.values()) for s in signals])

    def refresh_entities(self, gaz) -> None:
        with self.con:
            for e in gaz.entities.values():
                self.con.execute(
                    "INSERT INTO entities (entity_id, canonical_name, entity_type, aliases, country, sector, parent_entity) "
                    "VALUES (?,?,?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET canonical_name=excluded.canonical_name, "
                    "entity_type=excluded.entity_type, aliases=excluded.aliases, country=excluded.country, "
                    "sector=excluded.sector, parent_entity=excluded.parent_entity",
                    (e.id, e.name, e.type, json.dumps(list(e.aliases), ensure_ascii=False), e.country, e.sector, e.parent))
            self.con.execute("""
                UPDATE entities SET
                  item_count = (SELECT COUNT(*) FROM item_entities ie WHERE ie.entity_id = entities.entity_id),
                  event_count = (SELECT COUNT(DISTINCT i.event_id) FROM item_entities ie JOIN items i ON i.id = ie.item_id
                                 JOIN events ev ON ev.event_id = i.event_id
                                 WHERE ie.entity_id = entities.entity_id),
                  first_seen = (SELECT MIN(i.published_at) FROM item_entities ie JOIN items i ON i.id = ie.item_id
                                WHERE ie.entity_id = entities.entity_id),
                  last_seen = (SELECT MAX(i.published_at) FROM item_entities ie JOIN items i ON i.id = ie.item_id
                               WHERE ie.entity_id = entities.entity_id)
            """)


def item_dict(it: Item) -> dict:
    return asdict(it)
