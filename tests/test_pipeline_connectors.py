"""Connectors on fixture payloads shaped like the real formats, and failure isolation."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline.config import FIXTURES_DIR, SourceConfig, load_config
from app.pipeline.connectors import CONNECTORS, get_connector
from app.pipeline.connectors.base import FixtureTransport, Payload, SourceUnavailable, Transport

CFG = load_config()
FEEDS = FIXTURES_DIR / "feeds"


def collect(source_id: str):
    src = CFG.source(source_id)
    return get_connector(src.connector).collect(src, FixtureTransport(FEEDS))


def test_every_registry_connector_exists():
    for s in CFG.sources + load_config("sources.live.toml").sources:
        assert s.connector in CONNECTORS


def test_live_registry_only_enables_real_https_endpoints():
    for s in load_config("sources.live.toml").sources:
        if s.enabled:
            assert s.url.startswith("https://") and "REPLACE" not in s.url, s.id
        if "REPLACE" in s.url:
            assert not s.enabled, s.id


def test_rss_connector_returns_raw_items_without_business_logic():
    res = collect("fx-esa")
    today = [i for i in res.items if i.extra.get("guid", "").startswith("today-")]
    assert res.status == "ok" and len(today) == 2
    item = next(i for i in today if "Isar" in i.title)
    assert item.source == "fx-esa" and item.source_type == "institutional"
    assert "utm_source=rss" in item.url                      # URL untouched by the connector
    assert "<nav>" in item.raw_content                        # raw content kept as delivered
    assert item.published_at == "Tue, 06 Oct 2026 09:00:00 +0200"


def test_atom_connector():
    res = collect("fx-mbda")
    titles = [i.title for i in res.items]
    assert "MBDA wins French order for 40 counter-drone systems" in titles
    assert res.items[0].author == "MBDA Communications"


def test_google_news_splits_publisher_from_title():
    res = collect("fx-gn-space")
    item = next(i for i in res.items if i.extra.get("guid") == "today-gallium-gn")
    assert item.title == "EU weighs gallium stockpile as China curbs exports"
    assert item.extra["publisher"] == "Atlas Procurement Letter"
    assert item.extra["summary_is_link_list"]


def test_gdelt_connector_and_invalid_entry():
    res = collect("fx-gdelt")
    assert len(res.items) == 5 and res.status == "warning"
    assert any(code == "ENTRY_INVALID" for code, _ in res.warnings)
    assert res.items[0].published_at == "20261006T224500Z" and res.items[0].extra["metadata_only"]


def test_youtube_arxiv_hal_connectors():
    yt = collect("fx-yt-esa").items[0]
    assert yt.extra["video_id"] == "fixture0001" and "replay" in yt.summary.lower()
    ax = collect("fx-arxiv").items
    assert ax[0].url == "http://arxiv.example/abs/2610.04321v1" and ax[0].author == "J. Doe, R. Roe"
    hal = collect("fx-hal").items
    assert hal[0].language == "fr" and hal[1].published_at == "2026-13-45"


def test_malformed_xml_is_recovered_with_warning():
    res = collect("fx-thales")
    assert res.status == "warning" and res.items
    assert any(code == "XML_MALFORMED" for code, _ in res.warnings)


def test_empty_feed_is_a_warning_not_an_error():
    res = collect("fx-cnes")
    assert res.status == "warning" and not res.items and res.warnings[0][0] == "EMPTY_FEED"


def test_unreachable_source_is_an_error_not_an_exception():
    res = collect("fx-nasa")
    assert res.status == "error" and res.errors[0][0] == "SOURCE_UNREACHABLE" and not res.items


class _Garbage(Transport):
    mode = "test"

    def __init__(self, body: bytes):
        self.body = body

    def fetch(self, source):
        return [Payload(self.body, "test://garbage")]


class _Timeout(Transport):
    mode = "test"

    def fetch(self, source):
        raise SourceUnavailable("timeout after 15s")


@pytest.mark.parametrize("connector, body", [("rss", b"<html><body>502 Bad Gateway</body></html>"), ("rss", b""),
                                             ("gdelt", b"Your query was too short"), ("hal", b"{not json"),
                                             ("arxiv", b"\xff\xfe\x00garbage")])
def test_garbage_payloads_never_raise(connector, body):
    src = SourceConfig(id="t", name="t", connector=connector, source_type="press", family="PRS", tier=2)
    res = get_connector(connector).collect(src, _Garbage(body))
    assert res.status == "error" and res.errors and not res.items


def test_timeout_is_isolated():
    src = CFG.source("fx-esa")
    res = get_connector("rss").collect(src, _Timeout())
    assert res.status == "error" and "timeout" in res.errors[0][1]


def test_fixture_files_use_reserved_hosts_only():
    import re
    for path in Path(FEEDS).rglob("*"):
        if path.is_file():
            hosts = re.findall(r"https?://([a-zA-Z0-9.-]+)", path.read_text(encoding="utf-8", errors="replace"))
            real = [h for h in hosts if not h.endswith(".example") and h not in (
                "purl.org", "www.w3.org", "search.yahoo.com", "a9.com", "arxiv.org", "www.youtube.com")]
            # Namespace URIs only (never fetched); every content host is a reserved .example host.
            assert not real, (path.name, real[:3])
