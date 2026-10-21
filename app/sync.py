"""Free hosting: GitHub Actions does the work, the web server only reads.

GitHub Actions collects, analyses and writes the briefs, then publishes the database as a
release asset (``live.sqlite.gz`` on the release tagged ``data``). The web server (Render,
free plan) downloads it at start-up and then every few minutes, and swaps it in atomically.
Its own disk can be wiped at every sleep: nothing precious lives there.

    python -m app.sync export var/pipeline/live.sqlite live.sqlite.gz   # in the workflow
    python -m app.sync pull                                              # by hand, on the server

Environment: SECTOR_INTEL_GITHUB_REPO ("owner/name"), SECTOR_INTEL_GITHUB_TOKEN (a fine-grained
token: Actions read and write, Contents read), SECTOR_INTEL_SYNC=github to sync at start-up.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

from app.config import LIVE_DB

RELEASE_TAG = "data"
ASSET_NAME = "live.sqlite.gz"
WORKFLOW = "collecte.yml"
API = "https://api.github.com"


# ---------------------------------------------------------------------- export (workflow side)
def export(db: Path, out_gz: Path) -> int:
    """A consistent copy of the database (SQLite backup API, WAL included), gzip-compressed."""
    src = sqlite3.connect(str(db))
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "copy.sqlite"
        dst = sqlite3.connect(str(copy))
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.close()
        src.close()
        with copy.open("rb") as fi, gzip.open(out_gz, "wb", compresslevel=9) as fo:
            shutil.copyfileobj(fi, fo)
    return out_gz.stat().st_size


# ---------------------------------------------------------------------- GitHub API
class GitHubError(RuntimeError):
    pass


class GitHub:
    """The few GitHub REST calls the site needs. ``client`` is an httpx.Client (injectable in tests)."""

    def __init__(self, repo: str | None = None, token: str | None = None, client=None):
        self.repo = repo or os.getenv("SECTOR_INTEL_GITHUB_REPO", "")
        self.token = token if token is not None else os.getenv("SECTOR_INTEL_GITHUB_TOKEN", "")
        if not self.repo or "/" not in self.repo:
            raise GitHubError("SECTOR_INTEL_GITHUB_REPO must be set to owner/name")
        if client is None:
            import httpx  # noqa: PLC0415

            client = httpx.Client(timeout=30, follow_redirects=True)
        self.client = client

    def _headers(self, accept: str = "application/vnd.github+json") -> dict:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "space-intelligence"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, path: str, **params):
        res = self.client.get(f"{API}/repos/{self.repo}{path}", headers=self._headers(), params=params or None)
        if res.status_code == 404:
            return None
        if res.status_code != 200:
            raise GitHubError(f"GitHub HTTP {res.status_code} on {path}: {res.text[:160]}")
        return res.json()

    def asset(self, tag: str = RELEASE_TAG, name: str = ASSET_NAME) -> dict | None:
        rel = self._get(f"/releases/tags/{tag}")
        for a in (rel or {}).get("assets", []):
            if a.get("name") == name:
                return a
        return None

    def download(self, asset_id: int) -> bytes:
        res = self.client.get(f"{API}/repos/{self.repo}/releases/assets/{asset_id}",
                              headers=self._headers("application/octet-stream"))
        if res.status_code != 200:
            raise GitHubError(f"GitHub HTTP {res.status_code} downloading the database")
        return res.content

    def dispatch(self, workflow: str = WORKFLOW, ref: str = "main") -> None:
        res = self.client.post(f"{API}/repos/{self.repo}/actions/workflows/{workflow}/dispatches",
                               headers=self._headers(), json={"ref": ref})
        if res.status_code not in (201, 204):
            raise GitHubError(f"GitHub HTTP {res.status_code} starting the workflow: {res.text[:160]}")

    def runs(self, workflow: str = WORKFLOW, per_page: int = 5) -> list[dict]:
        data = self._get(f"/actions/workflows/{workflow}/runs", per_page=per_page)
        return (data or {}).get("workflow_runs", [])

    def steps(self, run_id: int) -> list[dict]:
        data = self._get(f"/actions/runs/{run_id}/jobs")
        jobs = (data or {}).get("jobs", [])
        return jobs[0].get("steps", []) if jobs else []


# ---------------------------------------------------------------------- pull (server side)
class Syncer:
    def __init__(self, github: GitHub, db: Path | None = None):
        self.gh = github
        self.db = db or LIVE_DB
        self.marker = self.db.with_suffix(".source.json")
        self.lock = threading.Lock()
        self.last_error = ""

    def _current(self) -> dict:
        try:
            return json.loads(self.marker.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def sync_once(self) -> bool:
        """Download the published database if it changed. True when a new one was installed."""
        with self.lock:
            asset = self.gh.asset()
            if asset is None:
                self.last_error = "no published database yet (the workflow has not run)"
                return False
            stamp = {"id": asset["id"], "updated_at": asset.get("updated_at", ""), "size": asset.get("size", 0)}
            if self.db.exists() and self._current() == stamp:
                return False
            blob = self.gh.download(asset["id"])
            self.db.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.db.parent, suffix=".sqlite")
            os.close(fd)
            try:
                Path(tmp).write_bytes(gzip.decompress(blob))
                con = sqlite3.connect(tmp)
                con.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()       # a real pipeline database
                con.close()
                os.replace(tmp, self.db)          # atomic: open readers keep the old file until they close
            except Exception:
                Path(tmp).unlink(missing_ok=True)
                raise
            self.marker.write_text(json.dumps(stamp))
            self.last_error = ""
            return True

    def run_forever(self, interval: int = 300) -> None:  # pragma: no cover - background loop
        while True:
            time.sleep(interval)
            try:
                self.sync_once()
            except Exception as exc:  # noqa: BLE001 - keep serving the last good database
                self.last_error = f"{exc.__class__.__name__}: {exc}"


_syncer: Syncer | None = None


def syncer() -> Syncer | None:
    return _syncer


def start_background_sync(interval: int = 300) -> Syncer | None:  # pragma: no cover - network
    """At start-up: first download right away, then a check every ``interval`` seconds."""
    global _syncer
    if os.getenv("SECTOR_INTEL_SYNC") != "github":
        return None
    try:
        _syncer = Syncer(GitHub())
        installed = _syncer.sync_once()
        print(f"sync: {'database downloaded' if installed else (_syncer.last_error or 'database already up to date')}",
              file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - the site starts anyway, with the "no data" page
        if _syncer is not None:
            _syncer.last_error = f"{exc.__class__.__name__}: {exc}"
        print(f"sync: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        if _syncer is None:
            return None
    threading.Thread(target=_syncer.run_forever, args=(interval,), name="db-sync", daemon=True).start()
    return _syncer


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - command line
    args = argv if argv is not None else sys.argv[1:]
    if args[:1] == ["export"] and len(args) == 3:
        size = export(Path(args[1]), Path(args[2]))
        print(f"{args[2]}: {size // 1024} Ko")
        return 0
    if args[:1] == ["pull"]:
        print("new database installed" if Syncer(GitHub()).sync_once() else "already up to date")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
