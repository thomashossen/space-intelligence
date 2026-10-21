"""Pipeline orchestration.

Item-level stages (normalise, clean, enrich, classify) run only on NEW raw items:
an entry already in ``raw_items`` is never reprocessed in incremental mode.
Set-level stages (deduplicate, cluster, score, relations, signals) are recomputed
over the analysis window (``window_days``) from what is stored, so incremental and
full-rebuild runs give identical results for the same inputs and as-of time.

One failing source never stops the run: its error is logged, its status is stored
and the other sources go on. Any other unexpected exception marks the run
``failed`` with the stage and message, and is re-raised for the CLI.
"""

from __future__ import annotations

import json
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.pipeline.classify import Classifier
from app.pipeline.config import FIXTURES_DIR, PipelineConfig, default_db, load_config
from app.pipeline.connectors import get_connector
from app.pipeline.connectors.base import FixtureTransport, Transport
from app.pipeline.dates import iso, parse_date
from app.pipeline.dedup import deduplicate
from app.pipeline.entities import Gazetteer, extract
from app.pipeline.events import build_event, cluster, numeric_facts
from app.pipeline.models import LogEntry, StageMetrics
from app.pipeline.normalize import normalize, raw_id
from app.pipeline.relations import build_relations
from app.pipeline.scoring import NoveltyContext, score_event, score_item
from app.pipeline.signals import SignalDetector
from app.pipeline.stats import Activity
from app.pipeline.store import Store
from app.pipeline.text import repeated_lines, strip_lines

UTC = timezone.utc
FIXTURE_AS_OF = datetime(2026, 10, 7, 6, 30, tzinfo=UTC)     # 08:30 Paris: the frozen morning of the test fixtures
WINDOW_DAYS = 120


@dataclass
class RunOptions:
    mode: str = "fixtures"                 # fixtures | live
    full_rebuild: bool = False
    sources: list[str] = field(default_factory=list)
    since: datetime | None = None
    db_path: Path | None = None          # None = default_db(mode)
    as_of: datetime | None = None
    fixtures_dir: Path = FIXTURES_DIR / "feeds"
    sources_file: str | None = None
    progress: object = None              # optional callable(stage: str, detail: str) for live progress

    @property
    def data_origin(self) -> str:
        return "fixture" if self.mode == "fixtures" else "live"


@dataclass
class RunResult:
    run_id: str
    status: str
    metrics: dict
    stages: list[dict]
    total_seconds: float
    logs: list[LogEntry]


def _transport(opts: RunOptions) -> Transport:
    if opts.mode == "fixtures":
        return FixtureTransport(opts.fixtures_dir)
    from app.pipeline.connectors.base import HttpTransport  # noqa: PLC0415 - live only

    return HttpTransport()


def run_pipeline(opts: RunOptions, cfg: PipelineConfig | None = None, transport: Transport | None = None) -> RunResult:
    cfg = cfg or load_config(opts.sources_file or ("sources.toml" if opts.mode == "fixtures" else "sources.live.toml"))
    transport = transport or _transport(opts)
    as_of = (opts.as_of or (FIXTURE_AS_OF if opts.mode == "fixtures" else datetime.now(UTC))).astimezone(UTC)
    collected_at = as_of if opts.mode == "fixtures" else datetime.now(UTC)   # fixtures: deterministic
    started = datetime.now(UTC)
    run_id = f"run-{started.strftime('%Y%m%dT%H%M%S')}-{int(time.perf_counter_ns() % 100000):05d}"
    t_run = time.perf_counter()
    if opts.db_path is None:
        opts.db_path = default_db(opts.mode)
    store = Store(opts.db_path)
    if opts.full_rebuild:
        store.reset()
    store.start_run({
        "run_id": run_id, "started_at": iso(started), "mode": "full_rebuild" if opts.full_rebuild else "incremental",
        "data_origin": opts.data_origin, "as_of": iso(as_of),
        "args": {"mode": opts.mode, "full_rebuild": opts.full_rebuild, "sources": opts.sources,
                 "since": iso(opts.since) if opts.since else None, "db": str(opts.db_path)},
        "processing_version": cfg.processing_version, "rules_version": cfg.rules_version,
        "model_version": cfg.model_version, "sources_file": cfg.sources_file,
    })
    logs: list[LogEntry] = []
    stages: list[StageMetrics] = []
    stage_name = "init"
    try:
        gaz = Gazetteer.from_config(cfg.entities)
        clf = Classifier(cfg.taxonomy, gaz)
        src_by_id = {s.id: s for s in cfg.sources}
        selected = [s for s in cfg.sources if s.enabled and (not opts.sources or s.id in opts.sources)]
        unknown = set(opts.sources) - set(src_by_id)
        for u in sorted(unknown):
            logs.append(LogEntry("collect", "error", "UNKNOWN_SOURCE", f"--source {u} is not in {cfg.sources_file}", u))
        for s in cfg.sources:
            if s.id in opts.sources and not s.enabled:
                logs.append(LogEntry("collect", "error", "SOURCE_DISABLED",
                                     f"--source {s.id} is disabled in {cfg.sources_file} (set enabled = true)", s.id))
        if not selected:
            logs.append(LogEntry("collect", "error", "NO_SOURCE_SELECTED",
                                 f"no enabled source to collect in {cfg.sources_file}: nothing was fetched"))

        # ---------------------------------------------------------------- COLLECT
        stage_name = "collect"
        m = StageMetrics("collect").start()
        collected = []
        source_status = Counter()
        for n_src, src in enumerate(selected, start=1):
            if opts.progress:
                opts.progress("collect", f"{n_src}/{len(selected)} {src.name}")
            conn = get_connector(src.connector)
            res = conn.collect(src, transport)
            if src.max_items and len(res.items) > src.max_items:
                # Keep the most recent entries so one high-volume feed cannot drown the others.
                dated = sorted(res.items, key=lambda r: parse_date(r.published_at, collected_at=collected_at,
                                                                     as_of=as_of)[0], reverse=True)
                logs.append(LogEntry("collect", "info", "CAPPED", f"{len(res.items)} entries, kept the {src.max_items} most recent", src.id))
                res.items = dated[: src.max_items]
            for code, msg in res.errors:
                logs.append(LogEntry("collect", "error", code, msg, src.id))
            for code, msg in res.warnings:
                logs.append(LogEntry("collect", "warning", code, msg, src.id))
            source_status[res.status] += 1
            m.add("sources_" + res.status)
            m.add("fetched", len(res.items))
            m.errors += len(res.errors)
            status = {"last_run_id": run_id, "last_status": res.status, "last_attempt_at": iso(started),
                      "last_error": (res.errors[0][0] + ": " + res.errors[0][1]) if res.errors else
                                    ((res.warnings[0][0] + ": " + res.warnings[0][1]) if res.warnings else None),
                      "items_last_run": len(res.items), "new_items_last_run": 0, "collect_seconds": res.seconds}
            if res.status != "error":
                status["last_success_at"] = iso(started)
            store.upsert_source(src, cfg.baseline(src), opts.data_origin, status)
            endpoint = res.endpoints[0] if res.endpoints else ""
            collected += [(src, r, endpoint) for r in res.items]
        for src in cfg.sources:
            if src not in selected:
                store.upsert_source(src, cfg.baseline(src), opts.data_origin)
        stages.append(m.stop())

        # ---------------------------------------------------------------- NORMALISE + CLEAN (new raw only)
        stage_name = "normalize"
        if opts.progress:
            opts.progress("normalize", "")
        m = StageMetrics("normalize_clean").start()
        ids = [raw_id(r) for _, r, _ in collected]
        known = store.known_raw_ids(ids)
        seen = set()
        new_items, raw_rows = [], []
        per_source_new = Counter()
        for (src, raw, endpoint), rid in zip(collected, ids):
            if rid in known or rid in seen:
                m.add("already_known")
                continue
            seen.add(rid)
            if opts.since:
                pub, _ = parse_date(raw.published_at, collected_at=collected_at, as_of=as_of)
                if pub < opts.since:
                    m.add("skipped_since")
                    continue
            raw_rows.append((rid, src.id, run_id, raw.url, raw.title, raw.summary, raw.raw_content, raw.author,
                             raw.language, raw.published_at, iso(collected_at), endpoint,
                             json.dumps(raw.extra, ensure_ascii=False, default=str)))
            item, item_logs = normalize(raw, src, cfg, collected_at=collected_at, as_of=as_of,
                                        data_origin=opts.data_origin, source_url=endpoint)
            logs += item_logs
            if item is None:
                m.add("rejected")
                continue
            new_items.append(item)
            per_source_new[src.id] += 1
        # Repeated paragraphs inside one source batch (footers, disclaimers).
        by_source = defaultdict(list)
        for it in new_items:
            by_source[it.source_id].append(it)
        for sid, its in by_source.items():
            rep = repeated_lines([it.cleaned_content for it in its])
            if rep:
                for it in its:
                    it.cleaned_content = strip_lines(it.cleaned_content, rep)
                logs.append(LogEntry("clean", "info", "REPEATED_BLOCKS_REMOVED", f"{len(rep)} repeated block(s) removed", sid))
        store.insert_raw(raw_rows)
        m.add("new_raw", len(raw_rows))
        m.add("cleaned", len(new_items))
        m.add("encoding_repaired", sum("encoding_repaired" in it.flags for it in new_items))
        m.add("date_estimated", sum("date_estimated" in it.flags for it in new_items))
        m.add("language_unknown", sum(it.language == "und" for it in new_items))
        for sid, n in per_source_new.items():
            store.con.execute("UPDATE sources SET new_items_last_run = ? WHERE source_id = ?", (n, sid))
        stages.append(m.stop())

        # ---------------------------------------------------------------- ENRICH + CLASSIFY (new items)
        stage_name = "enrich"
        if opts.progress:
            opts.progress("enrich", "")
        m = StageMetrics("enrich_classify").start()
        for it in new_items:
            extract(it, gaz, src_by_id[it.source_id].publisher_entity if it.source_id in src_by_id else "")
            clf.classify(it)
            it.numeric_facts = numeric_facts(it)
            m.add("with_entities", 1 if it.entity_ids else 0)
            m.add(it.processing_status)
        m.add("mentions", sum(len(it.mentions) for it in new_items))
        store.upsert_items(new_items)
        store.replace_item_entities(new_items)
        stages.append(m.stop())

        # ---------------------------------------------------------------- DEDUPLICATE (window)
        stage_name = "dedup"
        if opts.progress:
            opts.progress("dedup", "")
        m = StageMetrics("deduplicate").start()
        window_start = as_of - timedelta(days=WINDOW_DAYS)
        items = store.items_since(iso(window_start))
        # Items whose source left the registry (renamed, removed, or another data origin)
        # are kept in the database but left out of the analysis instead of crashing it.
        orphans = Counter(it.source_id for it in items if it.source_id not in src_by_id)
        for sid, n in sorted(orphans.items()):
            logs.append(LogEntry("deduplicate", "warning", "ORPHAN_SOURCE",
                                 f"{n} stored item(s) from {sid}, which is not in {cfg.sources_file}: ignored", sid))
        items = [it for it in items if it.source_id in src_by_id]
        counts = deduplicate(items, cfg.scoring["dedup"])
        for k, v in counts.items():
            m.add(k, v)
        m.add("window_items", len(items))
        stages.append(m.stop())

        # ---------------------------------------------------------------- CLUSTER EVENTS
        stage_name = "events"
        if opts.progress:
            opts.progress("events", "")
        m = StageMetrics("events").start()
        groups: dict[str, list] = defaultdict(list)
        for it in items:
            groups[it.duplicate_group_id].append(it)
        in_scope = {g for g, members in groups.items() if any(x.processing_status == "processed" for x in members)}
        heads = [it for it in items if it.is_origin_head and it.duplicate_group_id in in_scope]
        clusters, links = cluster(heads, cfg.scoring["events"])
        source_names = {s.id: s.name for s in cfg.sources}
        events = [build_event(c, groups, links, src_by_id, source_names) for c in clusters]
        events_by_id = {e.event_id: e for e in events}
        heads_by_event = {e.event_id: [x for x in heads if x.id in set(e.head_ids)] for e in events}
        item_event = {iid: e.event_id for e in events for iid in e.item_ids}
        for it in items:
            it.event_id = item_event.get(it.id, "")
        m.add("events", len(events))
        m.add("multi_origin", sum(len(e.origin_ids) > 1 for e in events))
        m.add("confirmed", sum(e.status == "confirmed" for e in events))
        m.add("candidate", sum(e.status == "candidate" for e in events))
        m.add("contested", sum(e.status == "contested" for e in events))
        stages.append(m.stop())

        # ---------------------------------------------------------------- SCORE
        stage_name = "score"
        if opts.progress:
            opts.progress("score", "")
        m = StageMetrics("score").start()
        entity_types = {eid: e.type for eid, e in gaz.entities.items()}
        sig_cfg = cfg.scoring["signals"]
        activity = Activity(events, as_of, sig_cfg["weeks"], entity_types)
        ctx = NoveltyContext(events, activity, entity_types)
        for ev in events:
            score_event(ev, heads_by_event[ev.event_id], src_by_id, cfg, ctx, as_of)
        for it in items:
            score_item(it, events_by_id.get(it.event_id), src_by_id[it.source_id], cfg)
            it.updated_at = iso(collected_at)
        m.add("events_scored", len(events))
        m.add("items_scored", len(items))
        m.add("priority_events", sum(e.explanation["importance"]["priority"] for e in events))
        stages.append(m.stop())

        # ---------------------------------------------------------------- RELATIONS
        stage_name = "relations"
        if opts.progress:
            opts.progress("relations", "")
        m = StageMetrics("relations").start()
        relations = build_relations(events, heads_by_event, gaz)
        m.add("relations", len(relations))
        m.add("pattern", sum(r["method"] == "pattern" for r in relations))
        stages.append(m.stop())

        # ---------------------------------------------------------------- SIGNALS
        stage_name = "signals"
        if opts.progress:
            opts.progress("signals", "")
        m = StageMetrics("signals").start()
        store.upsert_items(items)       # first_seen of sources needs the stored items
        labels = {eid: e.name for eid, e in gaz.entities.items()}
        labels.update({e.country: e.name for e in gaz.entities.values() if e.type in ("country", "region")})
        labels.update({r["relation_id"]: f"{labels.get(r['source_entity'])} → {labels.get(r['target_entity'])}" for r in relations})
        det = SignalDetector(cfg.scoring, activity, events_by_id, heads_by_event, store.source_first_seen(),
                             labels, as_of, opts.data_origin)
        signals = det.statistical() + det.new_entities(entity_types) + det.new_relations(relations, entity_types)
        if activity.history_weeks < sig_cfg["min_history_weeks"]:
            logs.append(LogEntry("signals", "warning", "INSUFFICIENT_HISTORY",
                                 f"{activity.history_weeks} baseline week(s) with events; {sig_cfg['min_history_weeks']} required"))
        for s in signals:
            m.add(s.kind)
        m.add("signals", len(signals))
        m.add("history_weeks", activity.history_weeks)
        stages.append(m.stop())

        # ---------------------------------------------------------------- STORE
        stage_name = "store"
        if opts.progress:
            opts.progress("store", "")
        m = StageMetrics("store").start()
        meta = {"processing_version": cfg.processing_version, "rules_version": cfg.rules_version,
                "model_version": cfg.model_version, "processed_at": iso(collected_at), "run_id": run_id,
                "data_origin": opts.data_origin}
        store.upsert_items(items)
        store.replace_events(iso(window_start), events, meta)
        rows = []
        by_id = {it.id: it for it in items}
        for e in events:
            for iid in e.item_ids:
                x = by_id[iid]
                rows.append((e.event_id, iid, x.duplicate_group_id, int(x.is_origin_head)))
        store.replace_event_items(rows)
        store.merge_relations(relations, cfg.rules_version)
        store.replace_signals(iso(as_of), signals, {
            "processing_version": cfg.processing_version, "rules_version": cfg.rules_version, "run_id": run_id,
            "data_origin": opts.data_origin, "detected_at": iso(collected_at)})
        store.refresh_entities(gaz)
        m.add("items", len(items))
        m.add("events", len(events))
        m.add("signals", len(signals))
        stages.append(m.stop())

        total = round(time.perf_counter() - t_run, 3)
        metrics = {
            "sources_selected": len(selected),
            "sources_ok": source_status["ok"], "sources_warning": source_status["warning"], "sources_error": source_status["error"],
            "ingested": stages[0].counts.get("fetched", 0),
            "new_raw": stages[1].counts.get("new_raw", 0),
            "rejected": stages[1].counts.get("rejected", 0),
            "cleaned": stages[1].counts.get("cleaned", 0),
            "out_of_scope": sum(it.processing_status == "out_of_scope" for it in items),
            "window_items": len(items),
            "duplicates": counts["duplicates"],
            "origins": counts["groups"],
            "events": len(events),
            "multi_origin_events": sum(len(e.origin_ids) > 1 for e in events),
            "contested_events": sum(e.status == "contested" for e in events),
            "entities_mentioned": len({mm["entity_id"] for it in items for mm in it.mentions}),
            "relations": len(relations),
            "signals": len(signals),
            "errors": sum(1 for l in logs if l.level == "error"),
            "warnings": sum(1 for l in logs if l.level == "warning"),
            "history_weeks": activity.history_weeks,
        }
        if not selected or (source_status["error"] and not (source_status["ok"] or source_status["warning"])):
            status = "failed"              # nothing collected at all: say so, never "ok"
        else:
            status = "ok" if not source_status["error"] else "partial"
        store.add_logs(run_id, iso(datetime.now(UTC)), logs)
        store.finish_run(run_id, finished_at=iso(datetime.now(UTC)), status=status, metrics=metrics,
                         stages=[s.as_dict() for s in stages], sources_ok=source_status["ok"],
                         sources_warning=source_status["warning"], sources_error=source_status["error"],
                         total_seconds=total,
                         error_summary="; ".join(f"{l.source_id}: {l.code}" for l in logs if l.level == "error")[:500] or None)
        return RunResult(run_id, status, metrics, [s.as_dict() for s in stages], total, logs)
    except Exception as exc:
        total = round(time.perf_counter() - t_run, 3)
        logs.append(LogEntry(stage_name, "error", "STAGE_FAILED", f"{exc.__class__.__name__}: {exc}\n{traceback.format_exc(limit=3)}"))
        store.add_logs(run_id, iso(datetime.now(UTC)), logs)
        store.finish_run(run_id, finished_at=iso(datetime.now(UTC)), status="failed", stages=[s.as_dict() for s in stages],
                         total_seconds=total, error_summary=f"{stage_name}: {exc.__class__.__name__}: {exc}"[:500])
        raise
    finally:
        store.close()
