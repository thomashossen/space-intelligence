"""French briefs (language model stage), source check and front-page variety, without network."""

from __future__ import annotations

import json
import shutil

import pytest

from app.pipeline.config import load_config
from app.pipeline.connectors.base import Payload, Transport
from app.pipeline.queries import Page, Reader
from app.pipeline.redaction import RedactionConfig, Writer, WriterError, article_text, parse_brief, run_redaction
from app.pipeline.runner import RunOptions, run_pipeline

GOOD = {"titre": "Titre en français", "resume": "Une phrase. Deux phrases. Trois phrases.",
        "points_cles": ["Fait 1", "Fait 2"], "enjeux": ["Industrie : explication."], "contexte": "Contexte.",
        "a_surveiller": ["Prochaine étape"], "sources_limitees": False}


class FakeWriter(Writer):
    name = "fake"

    def __init__(self, answer=None):
        super().__init__(RedactionConfig(provider="fake", model="fake-1"))
        self.calls, self.answer = [], answer

    def complete(self, system, user):
        self.calls.append(user)
        return self.answer if self.answer is not None else "Voici :\n" + json.dumps(GOOD, ensure_ascii=False)


@pytest.fixture()
def db(pipeline_run, tmp_path):
    path = tmp_path / "copy.sqlite"
    shutil.copy(pipeline_run[0], path)
    return path


def test_briefs_are_written_cached_and_exposed(db):
    cfg = RedactionConfig(provider="fake", max_events_per_run=3, fetch_articles=False)
    w = FakeWriter()
    st = run_redaction(db, cfg, writer=w, log=lambda m: None)
    assert st["written"] == 3 and len(w.calls) == 3
    assert "Source 1" in w.calls[0] and "Titre :" in w.calls[0]
    w2 = FakeWriter()
    again = run_redaction(db, cfg, writer=w2, log=lambda m: None)
    assert again["cached"] == 3 and again["written"] == 3              # reuses 3, budget goes to the next 3
    assert not set(w.calls) & set(w2.calls)
    r = Reader(db)
    top = r.events(sort="importance", page=Page.of(1, 1))["data"][0]
    assert top["brief"]["titre"] == "Titre en français" and top["brief"]["model"] == "fake-1"
    r.close()


def test_article_pages_are_used_when_longer(db):
    cfg = RedactionConfig(provider="fake", max_events_per_run=1, fetch_articles=True)
    w = FakeWriter()
    st = run_redaction(db, cfg, writer=w, fetch=lambda url, n: "Paragraphe complet de l'article. " * 20, log=lambda m: None)
    assert st["pages"] >= 1 and "Paragraphe complet" in w.calls[0]


def test_bad_answers_are_rejected_and_stop_after_three(db):
    cfg = RedactionConfig(provider="fake", max_events_per_run=10, fetch_articles=False)
    st = run_redaction(db, cfg, writer=FakeWriter("pas de JSON"), log=lambda m: None)
    assert st["written"] == 0 and st["failed"] == 3
    with pytest.raises(WriterError):
        parse_brief(json.dumps({**GOOD, "resume": ""}))
    assert parse_brief(json.dumps({**GOOD, "points_cles": "un seul"}))["points_cles"] == ["un seul"]


def test_article_text_keeps_the_article_body():
    html = "<html><nav>Accueil Menu</nav><article><h1>T</h1><p>" + "Le CNES a signé un contrat. " * 4 + "</p></article><footer>©</footer></html>"
    txt = article_text(html, 500)
    assert txt.startswith("Le CNES a signé") and "Menu" not in txt


def test_site_shows_brief_and_rule_based_reading(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    run_redaction(db, RedactionConfig(provider="fake", max_events_per_run=1, fetch_articles=False), writer=FakeWriter(), log=lambda m: None)
    monkeypatch.setenv("SECTOR_INTEL_PIPELINE_DB", str(db))
    c = TestClient(app)
    r = Reader(db)
    ranked = r.events(sort="importance", page=Page.of(1, 5))["data"]
    r.close()
    page = c.get(f"/evenements/{ranked[0]['id']}").text
    assert "Titre en français" in page and "Les enjeux" in page and "rédigée automatiquement par fake-1" in page
    other = c.get(f"/evenements/{ranked[1]['id']}").text
    assert "Ce que disent les sources" in other and "Pourquoi ça compte" in other


def test_front_page_variety():
    from app.services.site import pick_front_page

    def card(i, fam, src="a", kind="contract"):
        return {"families": fam, "lead_source": src, "event_type": kind, "i": i}
    cards = [card(1, ["AGG"], "gn"), card(2, ["AGG"], "gn2"), card(3, ["AGG"], "gn3"), card(4, ["RES"], "ax", "research"),
             card(5, ["RES"], "ax2", "research"), card(6, ["PRS"], "p"), card(7, ["PRS"], "p"), card(8, ["PRS"], "p"), card(9, ["INS"], "e")]
    assert [c["i"] for c in pick_front_page(cards)] == [1, 2, 4, 6, 7]


def test_max_items_keeps_most_recent(tmp_path):
    import dataclasses

    cfg = load_config("sources.toml")
    cfg.sources = [dataclasses.replace(s, max_items=5) if s.id == "fx-kestrel" else s for s in cfg.sources]
    res = run_pipeline(RunOptions(mode="fixtures", full_rebuild=True, db_path=tmp_path / "m.sqlite", sources=["fx-kestrel"]), cfg=cfg)
    assert res.metrics["ingested"] == 5
    assert any(l.code == "CAPPED" for l in res.logs)


def test_check_reports_and_disables(tmp_path, monkeypatch):
    from app.pipeline import check

    reg = tmp_path / "reg.toml"
    reg.write_text('version = "t"\n[[source]]\nid = "good"\nname = "G"\nconnector = "rss"\nsource_type = "press"\nfamily = "PRS"\ntier = 2\n'
                   'url = "https://g.example/feed"\nenabled = true\n\n[[source]]\nid = "bad"\nname = "B"\nconnector = "rss"\n'
                   'source_type = "press"\nfamily = "PRS"\ntier = 2\nurl = "https://b.example/feed"\nenabled = true\n')
    for name in ("entities.toml", "taxonomy.toml", "scoring.toml"):
        (tmp_path / name).write_bytes((check.CONFIG_DIR / name).read_bytes())
    monkeypatch.setattr(check, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(check, "load_config", lambda f: load_config(f, config_dir=tmp_path))
    rss = b'<rss version="2.0"><channel><item><title>Ariane 6 launch</title><link>https://g.example/a</link></item></channel></rss>'

    class T(Transport):
        mode = "live"

        def fetch(self, s):
            if s.id == "bad":
                from app.pipeline.connectors.base import SourceUnavailable
                raise SourceUnavailable("HTTP 404")
            return [Payload(rss, s.url)]
    rows = check.check_sources("reg.toml", transport=T())
    assert {r["id"]: r["status"] for r in rows} == {"good": "ok", "bad": "error"}
    assert check.disable(["bad"], "reg.toml") == ["bad"]
    assert [s.id for s in load_config("reg.toml", config_dir=tmp_path).sources if s.enabled] == ["good"]
    assert check.disabled_by_check("reg.toml") == ["bad"]
    assert check.reenable(["bad", "good"], "reg.toml") == ["bad"]
    assert [s.id for s in load_config("reg.toml", config_dir=tmp_path).sources if s.enabled] == ["good", "bad"]


def _compat(handler, monkeypatch, **cfg):
    import httpx

    from app.pipeline.redaction import OpenAICompatWriter

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    w = OpenAICompatWriter(RedactionConfig(provider="groq", **cfg), client=httpx.Client(transport=httpx.MockTransport(handler)))
    w.sleep = lambda s: None
    return w


def test_openai_compatible_writer_retries_429_and_parses(monkeypatch):
    import httpx

    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        assert req.headers["authorization"] == "Bearer test-key" and req.url.path == "/openai/v1/chat/completions"
        if len(seen) == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, text="slow down")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(GOOD)}}]})
    w = _compat(handler, monkeypatch)
    assert parse_brief(w.complete("sys", "user"))["titre"] == "Titre en français"
    assert len(seen) == 2 and seen[1]["model"] == "llama-3.3-70b-versatile"
    assert seen[1]["response_format"] == {"type": "json_object"} and seen[1]["messages"][0]["role"] == "system"


def test_openai_compatible_writer_drops_json_mode_when_refused(monkeypatch):
    import httpx

    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        if "response_format" in body:
            return httpx.Response(400, text='{"error": "response_format is not supported"}')
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(GOOD)}}]})
    w = _compat(handler, monkeypatch, model="other")
    w.complete("s", "u")
    assert "response_format" not in seen[-1] and len(seen) == 2


def test_openai_compatible_writer_needs_a_key(monkeypatch):
    from app.pipeline.redaction import make_writer

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    with pytest.raises(WriterError, match="MISTRAL_API_KEY"):
        make_writer(RedactionConfig(provider="mistral"))


def test_openai_compatible_writer_reports_quota(monkeypatch):
    import httpx

    w = _compat(lambda req: httpx.Response(429, text="quota"), monkeypatch)
    with pytest.raises(WriterError, match="429"):
        w.complete("s", "u")


class FakeTranslator:
    name = "fake-mt"

    def __init__(self):
        self.calls = []

    def translate(self, texts):
        self.calls.append(texts)
        return ["FR: " + t for t in texts]


def test_local_writer_extracts_and_translates(db):
    from app.pipeline.translate import LocalWriter

    tr = FakeTranslator()
    w = LocalWriter(translator=tr)
    page = ("The European Space Agency signed a contract worth 862 million euros with Thales Alenia Space. "
            "The Argonaut lander will deliver cargo to the Moon from 2030 onwards. Subscribe to our newsletter today please. "
            "The contract was signed in Seville during the ministerial council of the agency.")
    st = run_redaction(db, RedactionConfig(provider="local", max_events_per_run=1, fetch_articles=True), writer=w,
                       fetch=lambda url, n: page, log=lambda m: None)
    assert st["written"] == 1
    r = Reader(db)
    b = r.events(sort="importance", page=Page.of(1, 1))["data"][0]["brief"]
    r.close()
    assert b["method"] == "extractive" and b["translated"] and b["model"] == "fake-mt"
    assert b["titre"].startswith("FR: ") and b["resume"].startswith("FR: ")   # the longer text wins
    assert "newsletter" not in b["resume"] and b["enjeux"] == []


def test_local_writer_keeps_french_sources_untranslated():
    from app.pipeline.translate import LocalWriter

    tr = FakeTranslator()
    w = LocalWriter(translator=tr)
    head = {"id": "x", "title": "La DGA commande 40 systèmes anti-drones à MBDA", "language": "fr", "cleaned_content":
            "La Direction générale de l'armement a commandé quarante systèmes de lutte anti-drones à MBDA pour les armées. "
            "Les premières livraisons sont prévues pour la fin de l'année prochaine selon le ministère des Armées.",
            "summary": ""}
    b = w.brief(None, [head], {})
    assert not tr.calls and b["titre"].startswith("La DGA") and not b["translated"]


def test_translator_reports_missing_model(tmp_path):
    from app.pipeline.translate import Translator, TranslatorMissing, _package_root

    with pytest.raises(TranslatorMissing, match="translator install"):
        Translator(root=tmp_path)
    pkg = tmp_path / "translate-en_fr" / "translate-en_fr-1_9"
    (pkg / "model").mkdir(parents=True)
    (pkg / "model" / "model.bin").write_bytes(b"")
    assert _package_root(tmp_path / "translate-en_fr") == pkg


def test_site_shows_extractive_credit_and_reading(db, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.pipeline.translate import LocalWriter

    run_redaction(db, RedactionConfig(provider="local", max_events_per_run=1, fetch_articles=False),
                  writer=LocalWriter(translator=FakeTranslator()), log=lambda m: None)
    monkeypatch.setenv("SECTOR_INTEL_PIPELINE_DB", str(db))
    r = Reader(db)
    top = r.events(sort="importance", page=Page.of(1, 1))["data"][0]["id"]
    r.close()
    page = TestClient(app).get(f"/evenements/{top}").text
    assert "Phrases extraites" in page and "Rien n'est ajouté" in page and "Pourquoi ça compte" in page
    assert "Les enjeux" not in page



def test_all_missing_covers_every_event_and_reports_progress(db):
    from app.pipeline.translate import LocalWriter

    r = Reader(db)
    total_events = r.events(page=Page.of(1, 1))["total"]
    r.close()
    ticks = []
    cfg = RedactionConfig(provider="local", max_events_per_run=3, fetch_articles=False)
    st = run_redaction(db, cfg, writer=LocalWriter(translator=FakeTranslator()), log=lambda m: None, all_missing=True,
                       progress=lambda n, t, title: ticks.append((n, t)))
    assert st["pending"] == st["written"] > 3                           # not capped by max_events_per_run
    assert ticks[0] == (0, st["pending"]) and ticks[-1] == (st["pending"], st["pending"])
    again = run_redaction(db, cfg, writer=LocalWriter(translator=FakeTranslator()), log=lambda m: None, all_missing=True)
    assert again["pending"] == 0 and again["cached"] == st["written"]
    assert st["selected"] <= total_events


def test_redaction_stops_between_two_events(db):
    from app.pipeline.translate import LocalWriter

    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 2
    st = run_redaction(db, RedactionConfig(provider="local", fetch_articles=False), writer=LocalWriter(translator=FakeTranslator()),
                       log=lambda m: None, all_missing=True, should_stop=should_stop)
    assert st["written"] == 2 and st["stopped"] and st["pending"] > 2
