"""Free hosting: database published by GitHub Actions, refresh driven through the GitHub API."""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import time

import pytest

from app.services.refresh import GitHubRefreshJob
from app.sync import Syncer, export


class FakeGitHub:
    """Stands in for app.sync.GitHub: a release asset, a workflow, its runs and steps."""

    def __init__(self, blob: bytes | None = None):
        self.blob, self.asset_id, self.updated = blob, 1, "2026-10-14T10:00:00Z"
        self.dispatched = 0
        self.run_list: list[dict] = []
        self.step_list: list[dict] = []

    def asset(self):
        return None if self.blob is None else {"id": self.asset_id, "updated_at": self.updated, "size": len(self.blob)}

    def download(self, asset_id):
        return self.blob

    def dispatch(self, workflow="collecte.yml", ref="main"):
        self.dispatched += 1

    def runs(self, workflow="collecte.yml", per_page=5):
        return self.run_list

    def steps(self, run_id):
        return self.step_list


@pytest.fixture()
def published(pipeline_run, tmp_path):
    gz = tmp_path / "live.sqlite.gz"
    export(pipeline_run[0], gz)
    return gz.read_bytes()


def test_export_is_a_complete_gzipped_copy(pipeline_run, tmp_path):
    gz = tmp_path / "out.gz"
    export(pipeline_run[0], gz)
    raw = tmp_path / "raw.sqlite"
    raw.write_bytes(gzip.decompress(gz.read_bytes()))
    a = sqlite3.connect(pipeline_run[0]).execute("SELECT COUNT(*) FROM events").fetchone()[0]
    b = sqlite3.connect(raw).execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert a == b > 0 and gz.stat().st_size < raw.stat().st_size


def test_syncer_downloads_only_when_the_asset_changes(published, tmp_path):
    gh = FakeGitHub(published)
    db = tmp_path / "data" / "live.sqlite"
    s = Syncer(gh, db)
    assert s.sync_once() is True and db.exists()
    assert s.sync_once() is False                                    # same asset: nothing downloaded
    gh.asset_id, gh.updated = 2, "2026-10-14T10:15:00Z"
    assert s.sync_once() is True


def test_syncer_refuses_a_broken_download_and_keeps_the_old_base(published, tmp_path):
    gh = FakeGitHub(published)
    db = tmp_path / "live.sqlite"
    s = Syncer(gh, db)
    s.sync_once()
    before = db.read_bytes()
    gh.blob, gh.asset_id = gzip.compress(b"not a database"), 3
    with pytest.raises(sqlite3.DatabaseError):
        s.sync_once()
    assert db.read_bytes() == before and not list(tmp_path.glob("tmp*.sqlite"))


def test_syncer_without_published_base(tmp_path):
    s = Syncer(FakeGitHub(None), tmp_path / "live.sqlite")
    assert s.sync_once() is False and "not run" in s.last_error


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def test_github_refresh_dispatches_follows_steps_and_downloads(published, pipeline_run, tmp_path):
    db = tmp_path / "live.sqlite"
    shutil.copy(pipeline_run[0], db)
    con = sqlite3.connect(db)
    con.execute("UPDATE pipeline_runs SET started_at = '2020-01-01T00:00:00+00:00'")    # last run long ago
    con.execute("DELETE FROM events WHERE event_id IN (SELECT event_id FROM events LIMIT 3)")
    con.commit()
    con.close()
    now = [time.time()]
    gh = FakeGitHub(published)
    job = GitHubRefreshJob(github=gh, syncer=Syncer(gh, db), db=db, cooldown_s=900, clock=lambda: now[0])
    s = job.start()
    assert s["state"] == "running" and gh.dispatched == 1 and s["step"] == 1
    gh.run_list = [{"id": 7, "status": "in_progress", "created_at": _iso(now[0]), "run_started_at": _iso(now[0])}]
    gh.step_list = [{"name": "Set up job", "status": "completed"}, {"name": "Préparation", "status": "completed"},
                    {"name": "Collecte et analyse", "status": "in_progress"}, {"name": "Rédaction des fiches", "status": "queued"},
                    {"name": "Publication", "status": "queued"}]
    now[0] += 70
    s = job.status()
    assert s["step"] == 2 and s["label"] == "Collecte et analyse" and "depuis 1 min 10 s" in s["detail"] and 0 < s["percent"] < 100
    gh.step_list[2]["status"], gh.step_list[3]["status"] = "completed", "in_progress"
    now[0] += 6
    assert job.status()["label"] == "Rédaction des fiches"
    gh.run_list[0].update(status="completed", conclusion="success")
    now[0] += 6
    s = job.status()
    assert s["state"] == "done" and s["summary"]["new_events"] == 3 and "nouveaux événements" in s["message"]
    again = job.start()
    assert again["state"] == "cooldown" and gh.dispatched == 1


def test_github_refresh_follows_a_scheduled_run_instead_of_starting_another(tmp_path):
    gh = FakeGitHub(None)
    gh.run_list = [{"id": 9, "status": "in_progress", "created_at": _iso(time.time() - 120)}]
    job = GitHubRefreshJob(github=gh, syncer=Syncer(gh, tmp_path / "x.sqlite"), db=tmp_path / "x.sqlite", cooldown_s=900)
    assert job.start()["state"] == "running" and gh.dispatched == 0


def test_github_refresh_reports_a_failed_run(tmp_path):
    gh = FakeGitHub(None)
    now = [time.time()]
    job = GitHubRefreshJob(github=gh, syncer=Syncer(gh, tmp_path / "x.sqlite"), db=tmp_path / "x.sqlite",
                           cooldown_s=0, clock=lambda: now[0])
    job.start()
    gh.run_list = [{"id": 3, "status": "completed", "conclusion": "failure", "created_at": _iso(now[0]),
                    "html_url": "https://github.com/o/r/actions/runs/3"}]
    now[0] += 6
    s = job.status()
    assert s["state"] == "error" and "failure" in s["message"] and "actions/runs/3" in s["message"]


def test_github_mode_is_chosen_by_environment(monkeypatch):
    from app.services import refresh

    monkeypatch.setenv("SECTOR_INTEL_REFRESH_MODE", "github")
    assert isinstance(refresh._make_job(), GitHubRefreshJob)
    monkeypatch.delenv("SECTOR_INTEL_REFRESH_MODE")
    assert isinstance(refresh._make_job(), refresh.RefreshJob)


def test_start_up_sync_always_logs_a_line(monkeypatch, capsys):
    from app import sync

    monkeypatch.setenv("SECTOR_INTEL_SYNC", "github")
    monkeypatch.delenv("SECTOR_INTEL_GITHUB_REPO", raising=False)
    assert sync.start_background_sync() is None                    # misconfigured: the site still starts
    err = capsys.readouterr().err
    assert "sync: GitHubError" in err and "owner/name" in err
