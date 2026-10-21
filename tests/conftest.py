"""Shared fixtures: one full pipeline run on the fixture feeds, reused by integration and API tests."""

from __future__ import annotations

import pytest

from app.pipeline.runner import RunOptions, run_pipeline


@pytest.fixture(scope="session")
def pipeline_run(tmp_path_factory):
    db = tmp_path_factory.mktemp("pipeline") / "test.sqlite"
    result = run_pipeline(RunOptions(mode="fixtures", full_rebuild=True, db_path=db))
    return db, result
