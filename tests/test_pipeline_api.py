"""Data API over the pipeline database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.fixture()
def api(pipeline_run, monkeypatch):
    """Point the site and the API at the test database for the duration of a test."""
    db, res = pipeline_run
    monkeypatch.setenv("SECTOR_INTEL_PIPELINE_DB", str(db))
    return res


def test_missing_database_returns_503(tmp_path, monkeypatch):
    monkeypatch.setenv("SECTOR_INTEL_PIPELINE_DB", str(tmp_path / "nope.sqlite"))
    r = client.get("/api/events")
    assert r.status_code == 503 and "python -m app.pipeline run" in r.json()["detail"]


def test_fixture_notice_and_versions_on_every_list(api):
    for url in ("/api/items", "/api/events", "/api/signals", "/api/entities", "/api/sources", "/api/topics", "/api/metrics",
                "/api/pipeline/status", "/api/pipeline/runs"):
        body = client.get(url).json()
        assert body["data_origin"] == "fixture" and body["notice"].startswith("FIXTURE DATA"), url
        assert body["versions"]["rules"], url


def test_pagination_is_enforced(api):
    assert client.get("/api/items?page_size=1000").status_code == 422
    body = client.get("/api/items?page_size=5&page=2").json()
    assert body["page"] == 2 and len(body["data"]) == 5 and body["total"] > 100 and body["pages"] == (body["total"] + 4) // 5


def test_item_filters(api):
    by_company = client.get("/api/items?company=mbda&page_size=100").json()["data"]
    assert by_company and all(any(c["id"] == "mbda" for c in it["companies"]) for it in by_company)
    fr = client.get("/api/items?language=fr&since=2026-10-04").json()["data"]
    assert fr and all(it["language"] == "fr" for it in fr)
    dupes = client.get("/api/items?include_duplicates=true&page_size=1").json()["total"]
    heads = client.get("/api/items?page_size=1").json()["total"]
    assert dupes > heads
    oos = client.get("/api/items?status=out_of_scope").json()["data"]
    assert {i["title"] for i in oos} >= {"Regional bakery chain opens its 40th store"}
    hi = client.get("/api/items?min_credibility=80&page_size=100").json()["data"]
    assert all(i["scores"]["credibility"] >= 80 for i in hi)


def test_item_detail_has_raw_and_explanation(api):
    first = client.get("/api/items?q=Gallium squeeze&page_size=1").json()["data"][0]
    d = client.get(f"/api/items/{first['id']}").json()
    assert d["raw"]["raw_content"] and d["cleaned_content"] and d["raw"]["raw_id"]
    assert d["explanation"]["data_quality"]["components"] and d["classification"]
    assert d["versions"]["rules"] and d["dedup"]["duplicate_group_id"]
    assert client.get("/api/items/it-unknown").status_code == 404


def test_event_filters_and_detail_answer_the_why_questions(api):
    contested = client.get("/api/events?status=contested&since=2026-10-04").json()["data"]
    assert len(contested) == 1
    d = client.get(f"/api/events/{contested[0]['id']}").json()
    why = d["why"]
    assert why["important"] and why["new"] and why["credible"] and why["corroborated_by"]
    assert why["epistemic_status"].startswith("FACT, CONTESTED") and "6" in why["epistemic_status"]
    assert any("Contradictions" in line for line in why["credible"])                  # penalty always shown
    assert d["proof"] == {"src": 3, "orig": 3, "fam": 2, "families": ["INS", "PRS"]}
    assert {o["head"]["source_id"] for o in d["origins"]} == {"fx-esa", "fx-kestrel", "fx-rumour"}
    assert [r["met"] for r in d["confidence_rules"]][-1] is False
    space = client.get("/api/events?sector=space&since=2026-10-04&page_size=100").json()["data"]
    assert space and all("SPACE" in e["sectors"] for e in space)
    a6 = client.get("/api/events?q=Ariane 6 successfully").json()["data"][0]
    a6d = client.get(f"/api/events/{a6['id']}").json()
    assert sum(len(o["copies"]) for o in a6d["origins"]) == 4
    assert all(c["dedup_level"] for o in a6d["origins"] for c in o["copies"])


def test_signals_entities_sources_topics(api):
    sig = client.get("/api/signals?kind=surge").json()["data"]
    assert sig and all(s["kind"] == "surge" for s in sig)
    s0 = sig[0]
    assert s0["epistemic_note"].endswith("Not a forecast.") and s0["stats"]["weekly_series"] and s0["evidence"]
    assert s0["confidence"]["components"] and s0["caveats"]
    ent = client.get("/api/entities?q=Airbus").json()["data"]
    assert ent[0]["id"] == "airbus"
    d = client.get("/api/entities/maiaspace").json()
    assert d["parent"] == "arianegroup" and d["recent_events"] and d["signals"]
    src = client.get("/api/sources").json()["data"]
    nasa = next(s for s in src if s["id"] == "fx-nasa")
    assert nasa["status"] == "error" and "unreachable" in nasa["last_error"] and nasa["tier"] == 1
    assert [s["tier"] for s in src] == sorted(s["tier"] for s in src)
    topics = client.get("/api/topics").json()["data"]
    sc = next(t for t in topics if t["label"] == "SUPPLY_CHAIN")
    assert sc["signal"] == "surge"


def test_pipeline_status_runs_metrics(api):
    st = client.get("/api/pipeline/status").json()
    assert st["sources"]["error"] == 1 and st["sources"]["in_error"][0]["id"] == "fx-nasa"
    assert st["counts"]["duplicates"] > 0 and st["last_error"]["code"] == "SOURCE_UNREACHABLE"
    assert {s["stage"] for s in st["last_run"]["stages"]} >= {"collect", "deduplicate", "signals"}
    runs = client.get("/api/pipeline/runs").json()
    run = client.get(f"/api/pipeline/runs/{runs['data'][0]['run_id']}?level=warning").json()
    assert run["logs"]["data"] and all(l["level"] == "warning" for l in run["logs"]["data"])
    m = client.get("/api/metrics").json()
    assert m["items"]["dedup_levels"] and m["tables"]["raw_items"] >= m["tables"]["items"]
