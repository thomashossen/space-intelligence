"""Integration: fixture sources → collection → normalisation → … → SQLite, plus quality and
non-regression checks.

Quality is MEASURED against the ground truth of the fixtures (tests/pipeline_truth.py):
pairwise precision/recall of event clustering, and whether copies join their origin.
Thresholds are floors that a change must not break, not targets the code was tuned to hit.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.pipeline.config import load_config
from app.pipeline.connectors.base import FixtureTransport, SourceUnavailable
from app.pipeline.runner import RunOptions, run_pipeline
from tests.pipeline_truth import evaluate, item_keys

GOLDEN = Path(__file__).parent / "golden" / "pipeline_metrics.json"
GOLDEN_KEYS = ("sources_selected", "sources_ok", "sources_warning", "sources_error", "ingested", "new_raw", "rejected",
               "cleaned", "out_of_scope", "duplicates", "origins", "events", "multi_origin_events", "contested_events",
               "entities_mentioned", "relations", "signals", "history_weeks")


def con_of(db) -> sqlite3.Connection:
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def event_with(con, needle: str) -> sqlite3.Row:
    rows = con.execute("SELECT * FROM events WHERE title LIKE ? ORDER BY first_seen DESC", (f"%{needle}%",)).fetchall()
    assert rows, needle
    return rows[0]


# --------------------------------------------------------------------------- the run itself

def test_run_completes_and_isolates_failing_source(pipeline_run):
    db, res = pipeline_run
    m = res.metrics
    assert res.status == "partial"                                  # one source down ≠ failed run
    assert m["sources_error"] == 1 and m["sources_ok"] >= 15
    errors = [l for l in res.logs if l.level == "error"]
    assert [(l.source_id, l.code) for l in errors] == [("fx-nasa", "SOURCE_UNREACHABLE")]
    codes = {l.code for l in res.logs}
    assert {"XML_MALFORMED", "EMPTY_FEED", "DATE_INVALID", "DATE_FUTURE", "REJECTED_EMPTY", "TITLE_DERIVED",
            "ENCODING_REPAIRED", "LANGUAGE_UNKNOWN", "REPEATED_BLOCKS_REMOVED", "ENTRY_INVALID"} <= codes
    assert {s["stage"] for s in res.stages} == {"collect", "normalize_clean", "enrich_classify", "deduplicate", "events",
                                                "score", "relations", "signals", "store"}
    assert all(s["seconds"] >= 0 for s in res.stages)


def test_every_item_is_traceable(pipeline_run):
    db, res = pipeline_run
    con = con_of(db)
    orphans = con.execute("SELECT COUNT(*) FROM items i LEFT JOIN raw_items r ON r.raw_id = i.raw_id WHERE r.raw_id IS NULL").fetchone()[0]
    assert orphans == 0
    cfg = load_config()
    bad = con.execute("SELECT COUNT(*) FROM items WHERE processing_version != ? OR rules_version != ? OR model_version = '' "
                      "OR processed_at = '' OR source_id = '' OR canonical_url IS NULL", (cfg.processing_version, cfg.rules_version)).fetchone()[0]
    assert bad == 0
    run = con.execute("SELECT * FROM pipeline_runs WHERE run_id = ?", (res.run_id,)).fetchone()
    assert run["rules_version"] == cfg.rules_version and run["status"] == "partial" and json.loads(run["stages"])
    # raw content is never modified: the stored raw payload still has its navigation markup.
    raw = con.execute("SELECT raw_content FROM raw_items r JOIN items i ON i.raw_id = r.raw_id "
                      "WHERE i.title LIKE 'ESA selects Isar%'").fetchone()[0]
    assert "<nav>" in raw and "<nav>" not in con.execute("SELECT cleaned_content FROM items WHERE title LIKE 'ESA selects Isar%'").fetchone()[0]


def test_data_defects_are_handled(pipeline_run):
    db, _ = pipeline_run
    con = con_of(db)
    flags = lambda title: json.loads(con.execute("SELECT flags FROM items WHERE title LIKE ?", (title,)).fetchone()[0])  # noqa: E731
    assert "date_invalid" in flags("Arianespace books Vega C%") and "date_estimated" in flags("Arianespace books Vega C%")
    assert "date_future" in flags("Exercice de défense aérienne%")
    assert "encoding_repaired" in flags("Gallium squeeze%")
    assert "Défense" in con.execute("SELECT cleaned_content FROM items WHERE title LIKE 'Gallium squeeze%' AND source_id='fx-halden'").fetchone()[0]
    assert "title_derived" in flags("Big week for European launchers%")
    out = {r[0] for r in con.execute("SELECT title FROM items WHERE processing_status = 'out_of_scope'")}
    assert {"Regional bakery chain opens its 40th store", "🚀🚀🚀 soon", "Big week for European launchers."} <= out
    assert con.execute("SELECT COUNT(*) FROM items WHERE source_id='fx-rumour'").fetchone()[0] == 3   # the empty entry was rejected


# --------------------------------------------------------------------------- quality against ground truth

def test_dedup_and_clustering_quality_floors(pipeline_run):
    db, _ = pipeline_run
    q = evaluate(con_of(db))
    assert q["dedup_copies"]["copy_recall"] == 1.0
    assert q["dedup_copies"]["origins_wrongly_merged"] == 0
    assert q["events"]["precision"] >= 0.72
    assert q["events"]["recall"] >= 0.85


def test_key_events(pipeline_run):
    db, _ = pipeline_run
    con = con_of(db)
    mbda = event_with(con, "MBDA wins French order")
    assert json.loads(mbda["families"]) == ["IND", "INS", "PRS"] and len(json.loads(mbda["origin_ids"])) == 3
    assert mbda["status"] == "confirmed" and mbda["confidence_level"] == "high" and mbda["primary_source"] == 1
    links = json.loads(mbda["explanation"])["clustering"]["links"]
    assert any(l.get("cross_language") for l in links.values())        # French ministry ↔ English releases
    a6 = event_with(con, "Ariane 6 successfully launches")
    assert len(json.loads(a6["item_ids"])) == 7 and len(json.loads(a6["origin_ids"])) == 3   # 4 copies collapsed
    isar = event_with(con, "ESA selects Isar")
    assert isar["status"] == "contested" and isar["confidence_level"] == "low"
    contra = json.loads(isar["contradictions"])[0]
    assert contra["fact"] == "missions" and [v["value"] for v in contra["values"]] == [4.0, 6.0]
    assert contra["values"][0]["best_tier"] == 1 and contra["values"][1]["best_tier"] == 4


def test_thirty_reprints_do_not_count_thirty_times(pipeline_run):
    db, _ = pipeline_run
    con = con_of(db)
    a6 = event_with(con, "Ariane 6 successfully launches")
    novelty = {r["id"]: r["novelty_score"] for r in con.execute("SELECT id, novelty_score, is_origin_head FROM items WHERE event_id = ?",
                                                                (a6["event_id"],))}
    copies = [r["id"] for r in con.execute("SELECT id FROM items WHERE event_id = ? AND is_origin_head = 0", (a6["event_id"],))]
    assert copies and all(novelty[c] == 0 for c in copies)
    corr = next(c for c in json.loads(a6["explanation"])["credibility"]["components"] if c["key"] == "corroboration_bonus")
    assert "3 independent origin(s)" in corr["note"]


def test_expected_signals(pipeline_run):
    db, _ = pipeline_run
    con = con_of(db)
    sig = {(r["kind"], r["subject_id"]): r for r in con.execute("SELECT * FROM signals")}
    assert ("acceleration", "hypersonic") in sig and ("acceleration", "counter-uas") in sig
    assert ("surge", "gallium") in sig and ("drop", "business-jets") in sig
    assert ("new_entity", "maiaspace") in sig
    assert any(k == "new_relation" and "Isar" in r["subject_label"] and r["subject_label"].startswith("ESA") for (k, _), r in sig.items())
    assert not any(s == "LAUNCHERS" for _, s in sig)              # stable topic → no signal
    for r in sig.values():
        assert r["epistemic_status"] == "SIGNAL" and json.loads(r["caveats"]) and r["confidence_level"] in ("low", "moderate", "high")
        assert "forecast" not in r["statement"].lower()
    drop = sig[("drop", "business-jets")]
    assert drop["current"] == 0 and drop["p_value"] <= 0.05


def test_every_score_is_explained(pipeline_run):
    db, _ = pipeline_run
    con = con_of(db)
    for ev in con.execute("SELECT * FROM events"):
        expl = json.loads(ev["explanation"])
        for metric in ("importance", "credibility", "novelty", "strategic"):
            assert expl[metric]["value"] == ev[metric]
            assert expl[metric]["components"]
        strat = {c["key"] for c in expl["strategic"]["components"]}
        assert strat == {"geopolitical", "industrial", "technological", "supply_chain", "sovereignty"}
    for it in con.execute("SELECT explanation, data_quality_score, credibility_score, event_id FROM items"):
        expl = json.loads(it["explanation"])
        assert expl["data_quality"]["value"] == it["data_quality_score"]
        if it["event_id"]:
            assert expl["credibility"]["value"] == it["credibility_score"]


# --------------------------------------------------------------------------- incremental, rebuild, filters

def _snapshot(db) -> tuple:
    con = con_of(db)
    events = [tuple(r) for r in con.execute("SELECT event_id, importance, credibility, novelty, strategic, status FROM events ORDER BY event_id")]
    signals = [tuple(r) for r in con.execute("SELECT signal_id, confidence_score FROM signals ORDER BY signal_id")]
    items = [tuple(r) for r in con.execute("SELECT id, duplicate_group_id, event_id FROM items ORDER BY id")]
    return events, signals, items


def test_incremental_rerun_is_idempotent_and_full_rebuild_identical(tmp_path):
    db = tmp_path / "inc.sqlite"
    first = run_pipeline(RunOptions(mode="fixtures", full_rebuild=True, db_path=db))
    snap1 = _snapshot(db)
    second = run_pipeline(RunOptions(mode="fixtures", db_path=db))
    assert second.metrics["new_raw"] == 0 and second.metrics["cleaned"] == 0
    assert _snapshot(db) == snap1
    third = run_pipeline(RunOptions(mode="fixtures", full_rebuild=True, db_path=db))
    assert _snapshot(db) == snap1
    assert third.metrics["new_raw"] == first.metrics["new_raw"]
    con = con_of(db)
    assert con.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 3    # run history kept across rebuilds


def test_source_and_since_filters(tmp_path):
    db = tmp_path / "filtered.sqlite"
    res = run_pipeline(RunOptions(mode="fixtures", full_rebuild=True, db_path=db, sources=["fx-esa", "fx-kestrel", "nope"]))
    assert res.metrics["sources_selected"] == 2
    assert any(l.code == "UNKNOWN_SOURCE" for l in res.logs)
    con = con_of(db)
    assert {r[0] for r in con.execute("SELECT DISTINCT source_id FROM items")} == {"fx-esa", "fx-kestrel"}
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    db2 = tmp_path / "since.sqlite"
    res2 = run_pipeline(RunOptions(mode="fixtures", full_rebuild=True, db_path=db2, since=since))
    assert res2.metrics["cleaned"] < 100
    assert con_of(db2).execute("SELECT MIN(published_at) FROM items WHERE flags NOT LIKE '%date_estimated%'").fetchone()[0] >= since.isoformat()


def test_new_source_needs_config_only(tmp_path):
    """Adding a source = one registry block (+ a fixture file here). No code change."""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    base = Path(__file__).resolve().parent.parent / "config" / "pipeline"
    for f in ("entities.toml", "taxonomy.toml", "scoring.toml"):
        (cfg_dir / f).write_bytes((base / f).read_bytes())
    (cfg_dir / "sources.toml").write_text(
        'version = "t"\n[[source]]\nid = "new-feed"\nname = "New feed"\nconnector = "rss"\nsource_type = "press"\n'
        'family = "PRS"\ntier = 2\nfixture = ["kestrel.xml"]\n', encoding="utf-8")
    cfg = load_config("sources.toml", cfg_dir)
    res = run_pipeline(RunOptions(mode="fixtures", full_rebuild=True, db_path=tmp_path / "n.sqlite"), cfg=cfg)
    assert res.metrics["sources_ok"] == 1 and res.metrics["cleaned"] == 4


def test_unexpected_transport_failure_is_isolated(tmp_path):
    class Flaky(FixtureTransport):
        def fetch(self, source):
            if source.id == "fx-esa":
                raise RuntimeError("socket exploded")
            if source.id == "fx-kestrel":
                raise SourceUnavailable("timeout after 15s")
            return super().fetch(source)

    from app.pipeline.config import FIXTURES_DIR
    res = run_pipeline(RunOptions(mode="fixtures", full_rebuild=True, db_path=tmp_path / "f.sqlite"),
                       transport=Flaky(FIXTURES_DIR / "feeds"))
    errs = {(l.source_id, l.code) for l in res.logs if l.level == "error"}
    assert ("fx-esa", "TRANSPORT_ERROR") in errs and ("fx-kestrel", "SOURCE_UNREACHABLE") in errs
    assert res.metrics["events"] > 100


# --------------------------------------------------------------------------- golden non-regression

def test_golden_metrics(pipeline_run):
    """Any change in these counts must be deliberate: rerun with UPDATE_GOLDEN=1 and review the diff."""
    db, res = pipeline_run
    current = {k: res.metrics[k] for k in GOLDEN_KEYS}
    current["quality"] = evaluate(con_of(db))
    if os.getenv("UPDATE_GOLDEN") or not GOLDEN.exists():
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip("golden file written")
    assert current == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_item_keys_cover_today_fixtures(pipeline_run):
    db, _ = pipeline_run
    keys = set(item_keys(con_of(db)).values())
    assert "today-isar-rumour" in keys and "yt:video:fixture0001" in keys
