"""Plain-French score explanations, optional images, ecosystem drawing."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pipeline.queries import Page, Reader


@pytest.fixture()
def client(pipeline_run, monkeypatch):
    monkeypatch.setenv("SECTOR_INTEL_PIPELINE_DB", str(pipeline_run[0]))
    return TestClient(app)


def _explainer(r):
    from app.services.explain import Explainer
    from app.services.site import COUNTRIES, EVENT_TYPES, LABELS

    return Explainer(r.names, LABELS, COUNTRIES, {k: v[0] for k, v in EVENT_TYPES.items()})


def test_every_component_is_explained_in_french(pipeline_run):
    r = Reader(pipeline_run[0])
    ex = _explainer(r)
    ids = [e["id"] for e in r.events(sort="importance", page=Page.of(1, 60))["data"]]
    english = re.compile(r"\b(origin\(s\)|first seen|half-life|baseline|none detected|no rule|independent|famil|z-score|closest of|insufficient)\b")
    for i in ids:
        sections = ex.sections(r.event(i))
        assert [s["name"] for s in sections] == ["Importance", "Crédibilité", "Nouveauté", "Portée stratégique"]
        for s in sections:
            assert s["rows"] and s["level"] in ("faible", "moyen", "élevé", "prioritaire")
            for row in s["rows"]:
                assert row["text"] and not english.search(row["text"]), (i, row)
                assert re.fullmatch(r"([+−]\d+(,\d)? pts?|0 pt)", row["points"]), row["points"]
    r.close()


def test_importance_contributions_add_up(pipeline_run):
    r = Reader(pipeline_run[0])
    e = r.event(r.events(sort="importance", page=Page.of(1, 1))["data"][0]["id"])
    total = sum(c["contribution"] for c in e["explanation"]["importance"]["components"])
    assert abs(total - e["scores"]["importance"]) <= 1
    r.close()


def test_event_page_has_an_accordion_and_no_json_link(client, pipeline_run):
    r = Reader(pipeline_run[0])
    ev = r.events(sort="importance", page=Page.of(1, 1))["data"][0]["id"]
    r.close()
    html = client.get(f"/evenements/{ev}").text
    assert html.count('<details class="score') == 4 and "<summary>" in html
    assert "Détail du calcul" not in html and "/api/events/" not in html
    assert "Fiabilité des sources" in html and "Recoupement" in html and "Déclencheurs" in html


def test_ecosystem_lines_end_at_bubble_centres(client):
    html = client.get("/").text
    nodes = re.findall(r'class="eco__node" style="left: (\d+)%; top: (\d+)%"', html)
    ends = re.findall(r'<line x1="\d+" y1="\d+" x2="(\d+)" y2="(\d+)"/>', html)
    assert len(nodes) == 5 and ends and set(ends) <= set(nodes)
    assert 'viewBox="0 0 100 100"' in html


def test_images_are_optional_slots(tmp_path, monkeypatch, client):
    from app import images

    monkeypatch.setattr(images, "IMG_DIR", tmp_path)
    images._cache.update(key=None, data={})
    assert images.current() == {}
    html = client.get("/").text
    assert "hero--photo" not in html and "ev__art--photo" not in html              # drawn fallback
    for name in ("hero.webp", "hero.jpg", "mod-acteurs.jpg", "ev-contract.webp", "ev-contract-2.webp", "ev-launch.png",
                 "unrelated.webp", "ev-launch-9.png"):
        (tmp_path / name).write_bytes(b"x")
    have = images.available(tmp_path)
    assert have["hero"] == ["/static/img/hero.webp"]                                # webp preferred
    assert have["ev-contract"] == ["/static/img/ev-contract.webp", "/static/img/ev-contract-2.webp"]
    assert "unrelated" not in have and have["ev-launch"] == ["/static/img/ev-launch.png"]
    assert images.pick(have, "ev-contract", "ev-a") == images.pick(have, "ev-contract", "ev-a")   # stable per event
    images._cache.update(key=None, data={})
    html = client.get("/").text
    assert "hero--photo" in html and "url('/static/img/hero.webp')" in html
    assert "mod--photo" in html and "/static/img/mod-acteurs.jpg" in html


def test_prompts_cover_every_slot():
    from app import images

    for slot in images.SLOTS:
        p = images.prompt(slot)
        assert "no text" in p and "no logos" in p and "Aspect ratio" in p


def test_import_converts_and_never_enlarges(tmp_path, monkeypatch):
    pil = pytest.importorskip("PIL.Image")
    from app import images

    src, out = tmp_path / "src", tmp_path / "img"
    src.mkdir()
    pil.new("RGB", (1584, 672), (10, 20, 50)).save(src / "hero.jpeg")          # smaller than the 2400×1100 slot
    pil.new("RGB", (1200, 896), (10, 20, 50)).save(src / "ev-launch-2.jpeg")   # larger than the 640×480 slot
    (src / "notes.txt").write_text("x")
    monkeypatch.setattr(images, "IMG_DIR", out)
    assert images._import(src) == 0
    with pil.open(out / "hero.webp") as im:
        assert im.width <= 1584 and im.height <= 672 and abs(im.width / im.height - 2400 / 1100) < 0.01
    with pil.open(out / "ev-launch-2.webp") as im:
        assert im.size == (640, 480)
    assert not (out / "notes.webp").exists()
