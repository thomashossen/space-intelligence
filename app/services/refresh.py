"""Background refresh triggered by the "Actualiser" button.

One job at a time, in a thread of the web server:
  1. re-test the sources that `check` disabled, and switch back on those that answer again;
  2. collect and process every live source (same as `python -m app.pipeline run --live`);
  3. write the French briefs, if a provider is configured in redaction.toml.
Progress is published as a small state dict that the page polls. A cooldown between two
runs protects the sources (and their rate limits) from repeated clicks.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from app.pipeline.config import default_db

UTC = timezone.utc
COOLDOWN_S = int(os.getenv("SECTOR_INTEL_REFRESH_COOLDOWN", "180"))

STEPS = {1: "Vérification des sources", 2: "Collecte et analyse", 3: "Rédaction des fiches en français"}
STAGES = {
    "check": (1, "Revérification des sources désactivées"),
    "collect": (2, "Collecte des sources"),
    "normalize": (2, "Nettoyage des articles"),
    "enrich": (2, "Repérage des acteurs et des thèmes"),
    "dedup": (2, "Dédoublonnage"),
    "events": (2, "Regroupement en événements"),
    "score": (2, "Calcul des scores"),
    "relations": (2, "Relations entre acteurs"),
    "signals": (2, "Détection des signaux"),
    "store": (2, "Enregistrement"),
    "redact": (3, "Rédaction des fiches"),
}
# progress inside step 2 at the start of each processing stage (collect goes from 0 to 85)
STEP2 = {"normalize": 85, "enrich": 88, "dedup": 92, "events": 94, "score": 96, "relations": 97, "signals": 98, "store": 99}


def _event_ids(db) -> set[str]:
    if not db.exists():
        return set()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute("SELECT event_id FROM events")}
    except sqlite3.Error:
        return set()
    finally:
        con.close()


def _since_last_run(db) -> float | None:
    """Seconds since the last collection started, read from the database (survives restarts)."""
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT MAX(started_at) FROM pipeline_runs").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row or not row[0]:
        return None
    try:
        started = datetime.fromisoformat(row[0])
    except ValueError:
        return None
    return (datetime.now(UTC) - started.astimezone(UTC)).total_seconds()


def _recheck() -> list[str]:  # pragma: no cover - network
    from app.pipeline.check import check_sources, disabled_by_check, reenable  # noqa: PLC0415

    ids = disabled_by_check()
    if not ids:
        return []
    rows = check_sources(only=ids, include_disabled=True)
    return reenable([r["id"] for r in rows if r["status"] != "error"])


def _pipeline(progress):  # pragma: no cover - network
    from app.pipeline.runner import RunOptions, run_pipeline  # noqa: PLC0415

    return run_pipeline(RunOptions(mode="live", progress=progress))


def _redact(db, log, progress=None, should_stop=None):  # pragma: no cover - depends on the provider
    from app.pipeline.redaction import RedactionConfig, WriterError, run_redaction  # noqa: PLC0415

    cfg = RedactionConfig.load()
    if cfg.provider in ("", "none"):
        return None
    try:
        return run_redaction(db, cfg, log=log, all_missing=True, progress=progress, should_stop=should_stop)
    except WriterError as exc:
        log(f"redaction skipped: {exc}")
        return {"written": 0, "failed": 1, "error": str(exc)}


class RefreshJob:
    def __init__(self, *, recheck=_recheck, pipeline=_pipeline, redact=_redact, db=None, cooldown_s: int = COOLDOWN_S):
        self.recheck, self.pipeline, self.redact = recheck, pipeline, redact
        self.db = db
        self.cooldown_s = cooldown_s
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.state = {"state": "idle"}
        self._finished_mono = 0.0
        self._stop = False
        self._step_started = 0.0

    # ------------------------------------------------------------------ public
    def _wait(self) -> float:
        """Seconds before the next refresh may start: from this process and from the database,
        so a restart of the server does not reset the delay."""
        waits = [self.cooldown_s - (time.monotonic() - self._finished_mono)] if self._finished_mono else []
        since = _since_last_run(self.db or default_db("live"))
        if since is not None:
            waits.append(self.cooldown_s - since)
        return max([0.0, *waits])

    def status(self) -> dict:
        with self.lock:
            out = dict(self.state)
        out["cooldown_s"] = int(self._wait()) if out.get("state") != "running" else 0
        out["cooldown_min"] = self.cooldown_s // 60
        return out

    def start(self) -> dict:
        with self.lock:
            if self.state.get("state") == "running":
                return dict(self.state)
            wait = self._wait()
            if wait > 0:
                minutes = int(wait) // 60 + 1
                return {**self.state, "state": "cooldown", "cooldown_s": int(wait),
                        "message": f"Le site a été actualisé il y a moins de {max(1, self.cooldown_s // 60)} minutes. "
                                   f"Prochaine actualisation possible dans {minutes} min."}
            self._stop = False
            self.state = {"state": "running", "step": 1, "steps": 3, "step_label": STEPS[1], "stage": "check",
                          "label": STAGES["check"][1], "detail": "", "percent": 0, "done": 0, "total": 0, "eta_s": None,
                          "stopping": False, "started_at": datetime.now(UTC).isoformat(timespec="seconds")}
            self.thread = threading.Thread(target=self._run, name="sector-intel-refresh", daemon=True)
            self.thread.start()
            return dict(self.state)

    def stop(self) -> dict:
        """Ask the job to stop: the current step ends, then nothing else starts (briefs stop between two events)."""
        with self.lock:
            if self.state.get("state") == "running":
                self._stop = True
                self.state["stopping"] = True
            return dict(self.state)

    # ------------------------------------------------------------------ worker
    def _set(self, stage: str, detail: str = "", percent: float | None = None, done: int = 0, total: int = 0,
             eta_s: int | None = None) -> None:
        step, label = STAGES.get(stage, (0, stage))
        with self.lock:
            if self.state.get("step") != step:
                self._step_started = time.monotonic()
            self.state.update(stage=stage, step=step, step_label=STEPS.get(step, ""), label=label, detail=detail,
                              percent=round(percent if percent is not None else 0), done=done, total=total, eta_s=eta_s)

    def _collect_progress(self, stage: str, detail: str) -> None:
        if stage == "collect" and "/" in detail:
            n, rest = detail.split("/", 1)
            total = rest.split(" ", 1)[0]
            name = rest.split(" ", 1)[1] if " " in rest else ""
            try:
                n_i, t_i = int(n), int(total)
            except ValueError:
                n_i, t_i = 0, 0
            self._set("collect", name, 85 * (n_i - 1) / max(1, t_i), n_i, t_i)
        else:
            self._set(stage, "", STEP2.get(stage, 0))

    def _redact_progress(self, done: int, total: int, title: str) -> None:
        elapsed = time.monotonic() - self._step_started
        eta = int(elapsed / done * (total - done)) if done else None
        self._set("redact", title[:80], 100 * done / max(1, total), done, total, eta)

    def _run(self) -> None:
        db = self.db or default_db("live")
        summary: dict = {}
        try:
            self._set("check")
            summary["reenabled"] = self.recheck() or []
            before = _event_ids(db)
            self._set("collect", "", 0)
            res = self.pipeline(self._collect_progress)
            m = res.metrics
            summary.update(new_articles=m.get("new_raw", 0), sources_ok=m.get("sources_ok", 0) + m.get("sources_warning", 0),
                           sources=m.get("sources_selected", 0), sources_error=m.get("sources_error", 0),
                           run_status=res.status)
            summary["new_events"] = len(_event_ids(db) - before)
            if summary["run_status"] == "failed":
                raise RuntimeError("aucune source n'a répondu (connexion internet coupée ?)")
            st = None
            if not self._stop:
                self._set("redact", "Recherche des événements sans fiche", 0)
                st = self.redact(db, lambda msg: None, self._redact_progress, lambda: self._stop)
            summary["briefs"] = (st or {}).get("written", 0)
            summary["briefs_left"] = max(0, (st or {}).get("pending", 0) - (st or {}).get("written", 0))
            summary["stopped"] = self._stop
            if st and st.get("error"):
                summary["redaction_error"] = st["error"]
            with self.lock:
                self.state = {"state": "done", "percent": 100, "summary": summary, "message": _message(summary),
                              "finished_at": datetime.now(UTC).isoformat(timespec="seconds")}
        except Exception as exc:  # noqa: BLE001 - shown to the user, never crashes the server
            with self.lock:
                self.state = {"state": "error", "percent": self.state.get("percent", 0),
                              "message": f"L'actualisation a échoué pendant « {self.state.get('label', '')} » : "
                                         f"{exc.__class__.__name__}: {str(exc)[:160]}"}
        finally:
            self._finished_mono = time.monotonic()


def _plural(n: int, one: str, many: str) -> str:
    if n == 0:
        return f"aucun {one}"
    return f"{n} {one if n == 1 else many}"


def _message(s: dict) -> str:
    parts = [_plural(s.get("new_events", 0), "nouvel événement", "nouveaux événements"),
             _plural(s.get("new_articles", 0), "nouvel article", "nouveaux articles")]
    if s.get("briefs"):
        parts.append(_plural(s["briefs"], "fiche rédigée", "fiches rédigées"))
    text = ", ".join(parts) + f". {s.get('sources_ok', 0)} sources sur {s.get('sources', 0)} ont répondu."
    if s.get("stopped"):
        text += " Arrêté à votre demande"
        text += f" : {s['briefs_left']} fiche(s) restent à rédiger au prochain clic." if s.get("briefs_left") else "."
    elif s.get("briefs_left"):
        text += f" {s['briefs_left']} fiche(s) n'ont pas pu être rédigées."
    if s.get("redaction_error"):
        text += f" Rédaction impossible : {s['redaction_error']}."
    if s.get("reenabled"):
        text += " Source(s) rétablie(s) : " + ", ".join(s["reenabled"]) + "."
    return text


class GitHubRefreshJob:
    """Same interface as RefreshJob, for free hosting: the work runs on GitHub Actions.

    "Actualiser" starts the workflow (or follows the one already running, e.g. the 15-minute
    schedule), reports its steps as they progress, then downloads the new database.
    GitHub is asked at most every 5 seconds, however many visitors are watching.
    """

    POLL_S = 5.0
    STEP_OF = {"Collecte et analyse": (2, "Collecte et analyse"), "Rédaction des fiches": (3, "Rédaction des fiches"),
               "Publication": (3, "Publication de la base")}

    def __init__(self, github=None, syncer=None, db=None, cooldown_s: int = COOLDOWN_S, clock=time.time):
        self._gh, self._syncer = github, syncer
        self.db = db
        self.cooldown_s = cooldown_s
        self.clock = clock
        self.lock = threading.Lock()
        self.state = {"state": "idle"}
        self._dispatched_at = 0.0
        self._run_id = None
        self._before: set[str] = set()
        self._checked = 0.0

    @property
    def gh(self):
        if self._gh is None:
            from app.sync import GitHub  # noqa: PLC0415

            self._gh = GitHub()
        return self._gh

    @property
    def syncer(self):
        if self._syncer is None:
            from app.sync import Syncer, syncer  # noqa: PLC0415

            self._syncer = syncer() or Syncer(self.gh)
        return self._syncer

    def _db(self):
        return self.db or default_db("live")

    def _wait(self) -> float:
        waits = [self.cooldown_s - (self.clock() - self._dispatched_at)] if self._dispatched_at else []
        since = _since_last_run(self._db())
        if since is not None:
            waits.append(self.cooldown_s - since)
        return max([0.0, *waits])

    def _active_run(self) -> dict | None:
        for r in self.gh.runs():
            if r.get("status") in ("queued", "in_progress", "waiting", "requested", "pending"):
                return r
        return None

    def start(self) -> dict:
        with self.lock:
            if self.state.get("state") == "running":
                return dict(self.state)
            try:
                active = self._active_run()
                if active is None:
                    wait = self._wait()
                    if wait > 0:
                        minutes = int(wait) // 60 + 1
                        return {**self.state, "state": "cooldown", "cooldown_s": int(wait),
                                "message": f"Le site a été actualisé il y a moins de {max(1, self.cooldown_s // 60)} minutes. "
                                           f"Prochaine actualisation possible dans {minutes} min."}
                    self.gh.dispatch()
                    self._run_id = None
                else:
                    self._run_id = active["id"]                # a scheduled run is already working: follow it
            except Exception as exc:  # noqa: BLE001
                return {"state": "error", "message": f"GitHub ne répond pas : {exc}"}
            self._dispatched_at = self.clock()
            self._before = _event_ids(self._db())
            self._checked = 0.0
            self.state = {"state": "running", "step": 1, "steps": 3, "step_label": "Préparation sur GitHub",
                          "stage": "github", "label": "Démarrage sur GitHub Actions", "detail": "", "percent": 0,
                          "done": 0, "total": 0, "eta_s": None, "stopping": False}
            return dict(self.state)

    def stop(self) -> dict:
        return dict(self.state)                        # a workflow run is left to finish

    def status(self) -> dict:
        with self.lock:
            if self.state.get("state") == "running" and self.clock() - self._checked >= self.POLL_S:
                self._checked = self.clock()
                try:
                    self._poll()
                except Exception as exc:  # noqa: BLE001 - a hiccup: try again at the next poll
                    self.state["detail"] = f"GitHub injoignable ({exc.__class__.__name__}), nouvel essai…"
            out = dict(self.state)
        out["cooldown_s"] = int(self._wait()) if out.get("state") != "running" else 0
        out["cooldown_min"] = self.cooldown_s // 60
        return out

    def _poll(self) -> None:
        run = None
        if self._run_id is None:
            for r in self.gh.runs():
                created = r.get("created_at", "")
                try:
                    ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if ts >= self._dispatched_at - 60:
                    run, self._run_id = r, r["id"]
                    break
            if run is None:
                self.state.update(label="En attente du démarrage de GitHub Actions", detail="")
                return
        else:
            run = next((r for r in self.gh.runs(per_page=10) if r["id"] == self._run_id), None)
            if run is None:
                return
        if run.get("status") == "completed":
            self._finish(run)
            return
        steps = self.gh.steps(run["id"])
        named = [s for s in steps if s.get("name") in self.STEP_OF]
        current = next((s for s in steps if s.get("status") == "in_progress"), None)
        step, label = 1, "Préparation sur GitHub"
        if current and current.get("name") in self.STEP_OF:
            step, label = self.STEP_OF[current["name"]]
        elif any(s.get("status") == "completed" for s in named):
            done_named = [s for s in named if s.get("status") == "completed"]
            step, label = self.STEP_OF[done_named[-1]["name"]]
        finished = sum(1 for s in steps if s.get("status") == "completed")
        started = run.get("run_started_at") or run.get("created_at") or ""
        try:
            elapsed = int(self.clock() - datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp())
        except ValueError:
            elapsed = 0
        self.state.update(step=step, step_label=("Préparation sur GitHub", "Collecte et analyse",
                                                 "Rédaction des fiches et publication")[step - 1],
                          label=label, percent=round(100 * finished / max(1, len(steps))),
                          detail=f"sur GitHub Actions depuis {elapsed // 60} min {elapsed % 60:02d} s")

    def _finish(self, run: dict) -> None:
        if run.get("conclusion") != "success":
            self.state = {"state": "error", "percent": self.state.get("percent", 0),
                          "message": f"L'exécution GitHub s'est terminée en « {run.get('conclusion')} ». "
                                     f"Détail : {run.get('html_url', '')}"}
            return
        try:
            self.syncer.sync_once()
        except Exception as exc:  # noqa: BLE001
            self.state = {"state": "error", "message": f"Collecte terminée, mais la base n'a pas pu être téléchargée : {exc}"}
            return
        summary = {"new_events": len(_event_ids(self._db()) - self._before), "new_articles": 0,
                   "sources_ok": 0, "sources": 0}
        try:
            con = sqlite3.connect(f"file:{self._db()}?mode=ro", uri=True)
            row = con.execute("SELECT metrics FROM pipeline_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            con.close()
            m = json.loads(row[0]) if row and row[0] else {}
            summary.update(new_articles=m.get("new_raw", 0), sources=m.get("sources_selected", 0),
                           sources_ok=m.get("sources_ok", 0) + m.get("sources_warning", 0))
        except (sqlite3.Error, ValueError):
            pass
        self.state = {"state": "done", "percent": 100, "summary": summary, "message": _message(summary),
                      "finished_at": datetime.now(UTC).isoformat(timespec="seconds")}


def _make_job():
    if os.getenv("SECTOR_INTEL_REFRESH_MODE") == "github":
        return GitHubRefreshJob()
    return RefreshJob()


job = _make_job()
