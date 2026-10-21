"""Regressions found in the live audit (2026-10-14): a run with no enabled source must fail
loudly, a disabled --source must be reported, and items from a source that is not in the
registry (fixtures then live in one DB, or a renamed source) must not crash the run."""

from __future__ import annotations

import dataclasses

from app.pipeline.config import DEFAULT_DB, LIVE_DB, default_db, load_config
from app.pipeline.runner import RunOptions, run_pipeline


def test_live_and_fixtures_use_separate_databases():
    assert default_db("live") == LIVE_DB and default_db("fixtures") == DEFAULT_DB and LIVE_DB != DEFAULT_DB


def test_no_enabled_source_is_a_failed_run(tmp_path):
    cfg = load_config("sources.toml")
    cfg.sources = [dataclasses.replace(s, enabled=False) for s in cfg.sources]
    res = run_pipeline(RunOptions(mode="fixtures", full_rebuild=True, db_path=tmp_path / "a.sqlite"), cfg=cfg)
    assert res.status == "failed"
    assert "NO_SOURCE_SELECTED" in {l.code for l in res.logs}


def test_disabled_source_requested_is_reported(tmp_path):
    cfg = load_config("sources.toml")
    cfg.sources = [dataclasses.replace(s, enabled=s.id != "fx-esa") for s in cfg.sources]
    res = run_pipeline(RunOptions(mode="fixtures", full_rebuild=True, db_path=tmp_path / "b.sqlite", sources=["fx-esa"]), cfg=cfg)
    assert ("fx-esa", "SOURCE_DISABLED") in {(l.source_id, l.code) for l in res.logs}
    assert res.status == "failed"


def test_items_from_unknown_source_do_not_crash(tmp_path):
    db = tmp_path / "c.sqlite"
    run_pipeline(RunOptions(mode="fixtures", full_rebuild=True, db_path=db))
    cfg = load_config("sources.toml")
    cfg.sources = [s for s in cfg.sources if s.id != "fx-kestrel"]       # source removed from the registry
    res = run_pipeline(RunOptions(mode="fixtures", db_path=db), cfg=cfg)
    assert res.status in ("ok", "partial")
    assert ("fx-kestrel", "ORPHAN_SOURCE") in {(l.source_id, l.code) for l in res.logs}
    assert res.metrics["events"] > 0


def test_gdelt_empty_object_is_an_empty_feed_not_an_error():
    from app.pipeline.connectors.base import Payload, Transport
    from app.pipeline.connectors.gdelt import GdeltConnector

    class Empty(Transport):
        mode = "live"

        def fetch(self, source):
            return [Payload(b"{}", source.url)]

    src = next(s for s in load_config("sources.live.toml").sources if s.connector == "gdelt")
    res = GdeltConnector().collect(src, Empty())
    assert res.status == "warning" and not res.errors and res.warnings[0][0] == "EMPTY_FEED"
