"""Public site pages, rendered from a fixture pipeline database."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(pipeline_run, monkeypatch):
    db, _ = pipeline_run
    monkeypatch.setenv("SECTOR_INTEL_PIPELINE_DB", str(db))
    return TestClient(app)


def test_home_renders_pipeline_events(client, pipeline_run):
    html = client.get("/").text
    assert len(re.findall(r"<h1[\s>]", html)) == 1
    assert "À la une" in html and "Données fictives" in html          # fixture DB is labelled
    assert html.count('class="ev"') >= 4
    assert 'href="/evenements/' in html


def test_news_search_filters_and_pages(client):
    all_ = client.get("/actualites").text
    assert "événements" in all_ and "Page suivante" in all_
    hit = client.get("/actualites?q=Ariane").text
    assert "Ariane" in hit and "pour « Ariane »" in hit
    none = client.get("/actualites?q=zzzzzz").text
    assert "Aucun événement ne correspond" in none
    assert client.get("/actualites?type=launch&scope=space&sort=importance&theme=launchers").status_code == 200


def test_event_page_links_to_sources(client):
    html = client.get("/actualites?sort=importance").text
    event_id = re.search(r'href="/evenements/([^"]+)"', html).group(1)
    page = client.get(f"/evenements/{event_id}").text
    assert "Sources (" in page and "Les scores" in page and "Ce que disent les sources" in page and 'rel="noopener"' in page
    assert client.get("/evenements/nope").status_code == 404


def test_trends_tiles_and_signals(client):
    html = client.get("/tendances").text
    assert "Secteurs" in html and "Thèmes" in html and 'class="ttile' in html and "<polyline" in html
    assert 'href="/actualites?theme=' in html and "Signaux détectés" in html


def test_actor_counts_match_their_page(client, pipeline_run):
    from app.pipeline.queries import Page, Reader

    r = Reader(pipeline_run[0])
    ents = r.entities(min_events=1, page=Page.of(1, 100))["data"]
    r.close()
    index = client.get("/actualites").status_code == 200 and client.get("/acteurs?type=country&sort=rising").text
    assert "Pays" in index and 'href="/acteurs/country-' in index
    for e in ents[:12] + [x for x in ents if x["type"] == "country"][:3]:
        page = client.get(f"/acteurs/{e['id']}").text
        n = e["event_count"]
        assert f"{n} événement" in page, e["id"]
        assert page.count('class="ev"') == min(n, 15), e["id"]          # the count is what you can read
    assert client.get("/acteurs/nope").status_code == 404


def test_actor_page_shows_activity_and_related(client):
    html = client.get("/acteurs/esa").text
    assert "Activité" in html and 'class="bars"' in html and "Souvent cité avec" in html and "/acteurs/" in html
    assert "Agence" in html or "ESA" in html


def test_countries_are_named_in_french(client):
    assert "États-Unis" in client.get("/acteurs?type=country").text or "Allemagne" in client.get("/acteurs?type=country").text


def test_missing_database_explains_what_to_do(monkeypatch, tmp_path):
    monkeypatch.setenv("SECTOR_INTEL_PIPELINE_DB", str(tmp_path / "absent.sqlite"))
    res = TestClient(app).get("/")
    assert res.status_code == 503 and "Cliquez sur « Actualiser »" in res.text          # local: the button is allowed


def test_footer_links_and_refresh_button(client):
    html = client.get("/").text
    assert 'href="/conditions"' in html and 'href="/docs"' in html
    assert "Prototype Brief" not in html and "État du pipeline" not in html
    assert "data-refresh" in html and "Actualiser" in html and "/static/js/site.js" in html
    assert 'data-refresh-part="main"' in html and 'data-refresh-part="foot"' in html


def test_conditions_page_is_a_marked_draft(client):
    html = client.get("/conditions").text
    assert "Conditions d'utilisation" in html and "Projet à compléter" in html and "[adresse de contact]" in html


def test_detokenize_removes_sentencepiece_markers():
    from app.pipeline.translate import detokenize

    out = detokenize(["▁L", "'", "Agence", "▁spatiale", "▁a", "▁signé", "▁un", "▁contrat", "."])
    assert out == "L'Agence spatiale a signé un contrat." and "▁" not in out



def test_refresh_api_runs_in_background(client, monkeypatch):
    import time

    from app.services import refresh

    calls = []

    class Res:
        status, metrics = "ok", {"new_raw": 12, "sources_ok": 17, "sources_warning": 1, "sources_selected": 18}

    def pipeline(progress):
        progress("collect", "3/18 SpaceNews")
        calls.append("pipeline")
        return Res()

    def redact(db, log, progress, should_stop):
        progress(0, 1, "")
        progress(1, 1, "Une fiche")
        return {"written": 1, "pending": 1}

    job = refresh.RefreshJob(recheck=lambda: ["air-cosmos"], pipeline=pipeline, redact=redact, cooldown_s=60)
    monkeypatch.setattr(refresh, "job", job)
    res = client.post("/api/refresh")
    assert res.status_code in (200, 202) and res.json()["state"] in ("running", "done")
    for _ in range(50):
        s = client.get("/api/refresh").json()
        if s["state"] != "running":
            break
        time.sleep(0.05)
    assert s["state"] == "done" and calls == ["pipeline"] and s["allowed"]
    assert "12 nouveaux articles" in s["message"] and "1 fiche rédigée" in s["message"] and "air-cosmos" in s["message"]
    again = client.post("/api/refresh").json()
    assert again["state"] == "cooldown" and again["cooldown_s"] > 0


def test_refresh_reports_errors(monkeypatch):
    import time

    from app.services import refresh

    def boom(progress):
        raise RuntimeError("network down")
    job = refresh.RefreshJob(recheck=lambda: [], pipeline=boom, redact=lambda *a: None, cooldown_s=0)
    job.start()
    for _ in range(50):
        if job.status()["state"] != "running":
            break
        time.sleep(0.05)
    s = job.status()
    assert s["state"] == "error" and "network down" in s["message"] and "Collecte" in s["message"]


def test_refresh_is_refused_from_another_machine(pipeline_run, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("SECTOR_INTEL_PIPELINE_DB", str(pipeline_run[0]))
    remote = TestClient(app, client=("203.0.113.7", 50000))
    assert remote.post("/api/refresh").status_code == 403
    assert remote.get("/api/refresh").json()["allowed"] is False



def test_refresh_reports_steps_progress_and_can_stop(monkeypatch):
    import threading
    import time

    from app.services import refresh

    gate, seen = threading.Event(), {}

    class Res:
        status, metrics = "ok", {"new_raw": 3, "sources_ok": 2, "sources_selected": 2}

    def pipeline(progress):
        progress("collect", "1/2 ESA")
        return Res()

    def redact(db, log, progress, should_stop):
        progress(0, 5, "")
        for n in range(1, 6):
            if should_stop():
                return {"written": n - 1, "pending": 5}
            progress(n, 5, f"Fiche {n}")
            if n == 2:
                seen["mid"] = job.status()
                gate.wait(2)
        return {"written": 5, "pending": 5}
    job = refresh.RefreshJob(recheck=lambda: [], pipeline=pipeline, redact=redact, cooldown_s=0)
    job.start()
    for _ in range(100):
        if "mid" in seen:
            break
        time.sleep(0.02)
    mid = seen["mid"]
    assert mid["step"] == 3 and mid["steps"] == 3 and mid["done"] == 2 and mid["total"] == 5
    assert mid["step_label"] == "Rédaction des fiches en français" and mid["detail"] == "Fiche 2" and mid["eta_s"] is not None
    assert job.stop()["stopping"] is True
    gate.set()
    for _ in range(100):
        if job.status()["state"] != "running":
            break
        time.sleep(0.02)
    s = job.status()
    assert s["state"] == "done" and s["summary"]["briefs"] == 2 and s["summary"]["stopped"]
    assert "Arrêté à votre demande" in s["message"] and "3 fiche(s) restent" in s["message"]


def test_stop_endpoint(client, monkeypatch):
    from app.services import refresh

    monkeypatch.setattr(refresh, "job", refresh.RefreshJob(recheck=lambda: [], pipeline=None, redact=None))
    assert client.post("/api/refresh/stop").json()["state"] == "idle"


def test_header_is_sticky_and_panel_has_steps(client):
    html = client.get("/").text
    assert "data-refresh-step" in html and "data-refresh-stop" in html
    css = client.get("/static/css/site.css").text
    assert "position: sticky; top: 0" in css



def test_collection_status_page(client):
    html = client.get("/collecte").text
    for text in ("État de la collecte", "Dernière collecte", "Sources", "Erreurs et avertissements",
                 "Source injoignable", "Historique des collectes", "Fiches en français"):
        assert text in html, text
    home = client.get("/").text
    assert 'href="/collecte"' in home


def test_old_addresses_redirect_to_the_new_pages(client):
    for old, new in (("/trends", "/tendances"), ("/brief", "/"), ("/pipeline", "/collecte"), ("/actors", "/acteurs"),
                     ("/signals", "/tendances"), ("/events", "/actualites")):
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == new, old


def test_unknown_page_uses_the_site_design(client):
    r = client.get("/nimporte-quoi")
    assert r.status_code == 404 and "Cette page n'existe pas" in r.text and "site.css" in r.text
    assert client.get("/api/nope").json() == {"detail": "Not Found"}


def test_old_interface_is_gone(client):
    for url in ("/partials/brief", "/api/demo/brief", "/api/suggest?q=a", "/static/css/brief.css", "/static/js/app.js", "/design-system"):
        assert client.get(url, follow_redirects=False).status_code == 404, url


def test_site_never_falls_back_to_test_data(monkeypatch, tmp_path):
    from app.config import LIVE_DB, pipeline_db

    monkeypatch.delenv("SECTOR_INTEL_PIPELINE_DB", raising=False)
    assert pipeline_db() == LIVE_DB and LIVE_DB.name == "live.sqlite"


def test_public_refresh_waits_fifteen_minutes_even_after_a_restart(pipeline_run, monkeypatch, tmp_path):
    """The delay is read from the database: restarting the server does not reset it."""
    import shutil
    import sqlite3
    from datetime import datetime, timedelta, timezone

    from app.services import refresh

    db = tmp_path / "live.sqlite"
    shutil.copy(pipeline_run[0], db)
    con = sqlite3.connect(db)
    five_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    con.execute("UPDATE pipeline_runs SET started_at = ? WHERE run_id = (SELECT run_id FROM pipeline_runs ORDER BY started_at DESC LIMIT 1)",
                (five_min_ago,))
    con.commit()
    con.close()
    fresh = refresh.RefreshJob(recheck=lambda: [], pipeline=None, redact=None, db=db, cooldown_s=900)   # a new process
    s = fresh.start()
    assert s["state"] == "cooldown" and 590 <= s["cooldown_s"] <= 600
    assert "Prochaine actualisation possible dans 10 min" in s["message"]
    assert fresh.status()["cooldown_s"] > 0 and fresh.status()["cooldown_min"] == 15


def test_open_refresh_lets_visitors_start_but_not_stop(pipeline_run, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import refresh

    monkeypatch.setenv("SECTOR_INTEL_PIPELINE_DB", str(pipeline_run[0]))
    monkeypatch.setenv("SECTOR_INTEL_REFRESH", "open")
    monkeypatch.setattr(refresh, "job", refresh.RefreshJob(recheck=lambda: [], pipeline=None, redact=None, cooldown_s=900,
                                                           db=pipeline_run[0]))
    visitor = TestClient(app, client=("198.51.100.4", 4000))
    s = visitor.get("/api/refresh").json()
    assert s["allowed"] is True and s["can_stop"] is False
    assert visitor.post("/api/refresh/stop").status_code == 403


def test_healthz_and_missing_page_in_public_mode(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("SECTOR_INTEL_PIPELINE_DB", str(tmp_path / "none.sqlite"))
    monkeypatch.setenv("SECTOR_INTEL_REFRESH", "open")
    c = TestClient(app, client=("198.51.100.4", 4000))
    assert c.get("/healthz").json() == {"ok": True}
    html = c.get("/").text
    assert "Cliquez sur « Actualiser »" in html and "python -m app.pipeline" not in html


def test_data_dir_moves_database_and_model(monkeypatch, tmp_path):
    import importlib

    import app.config as cfg

    monkeypatch.setenv("SECTOR_INTEL_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SECTOR_INTEL_PIPELINE_DB", raising=False)
    reloaded = importlib.reload(cfg)
    try:
        assert reloaded.LIVE_DB == tmp_path / "pipeline" / "live.sqlite" and reloaded.pipeline_db() == reloaded.LIVE_DB
    finally:
        monkeypatch.delenv("SECTOR_INTEL_DATA_DIR")
        importlib.reload(cfg)


def test_render_blueprint_is_free_and_consistent():
    yaml = pytest.importorskip("yaml")
    from app.config import BASE_DIR

    web = yaml.safe_load((BASE_DIR / "render.yaml").read_text())["services"][0]
    env = {e["key"]: e.get("value") for e in web["envVars"]}
    assert web["plan"] == "free" and "disk" not in web and web["healthCheckPath"] == "/healthz"
    assert env["SECTOR_INTEL_SYNC"] == "github" and env["SECTOR_INTEL_REFRESH_MODE"] == "github"
    assert env["SECTOR_INTEL_REFRESH"] == "open" and env["SECTOR_INTEL_REFRESH_COOLDOWN"] == "900"
    secrets = [e for e in web["envVars"] if e["key"] in ("SECTOR_INTEL_GITHUB_REPO", "SECTOR_INTEL_GITHUB_TOKEN")]
    assert len(secrets) == 2 and all(e.get("sync") is False and "value" not in e for e in secrets)   # never in the repo
    assert "--port $PORT" in web["startCommand"] and "proxy-headers" not in web["startCommand"]


def test_workflow_matches_what_the_site_expects():
    yaml = pytest.importorskip("yaml")
    from app.config import BASE_DIR
    from app.services.refresh import GitHubRefreshJob
    from app.sync import ASSET_NAME, RELEASE_TAG, WORKFLOW

    wf = yaml.safe_load((BASE_DIR / ".github" / "workflows" / WORKFLOW).read_text())
    triggers = wf[True]                                           # YAML 1.1 reads the key "on" as True
    assert "workflow_dispatch" in triggers and triggers["schedule"][0]["cron"] == "*/15 * * * *"
    assert wf["concurrency"]["cancel-in-progress"] is False and wf["permissions"]["contents"] == "write"
    names = [s.get("name") for s in wf["jobs"]["collecte"]["steps"]]
    for step in GitHubRefreshJob.STEP_OF:
        assert step in names, step                                # progress labels follow these step names
    publish = wf["jobs"]["collecte"]["steps"][-1]["run"]
    assert f"gh release upload {RELEASE_TAG} {ASSET_NAME}" in publish and "python -m app.sync export" in publish
