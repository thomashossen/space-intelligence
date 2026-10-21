"""Unit tests: URL, dates, cleaning, language, dedup, entities, classification, scoring, statistics."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.pipeline.classify import Classifier
from app.pipeline.config import load_config
from app.pipeline.dates import parse_date
from app.pipeline.dedup import deduplicate, proper_noun_swap
from app.pipeline.entities import Gazetteer, extract
from app.pipeline.events import conflicts, numeric_facts, pair_similarity
from app.pipeline.models import Item
from app.pipeline.scoring import credibility, data_quality, importance
from app.pipeline.similarity import TfIdf, jaccard, tokens
from app.pipeline.stats import poisson_cdf, series_stats
from app.pipeline.text import clean_text, detect_language, fix_mojibake, make_summary, repeated_lines
from app.pipeline.urls import canonicalize

UTC = timezone.utc
NOW = datetime(2026, 10, 7, 6, 30, tzinfo=UTC)
CFG = load_config()
GAZ = Gazetteer.from_config(CFG.entities)
CLF = Classifier(CFG.taxonomy, GAZ)


def make_item(id_: str, title: str, *, content: str = "", published: str = "2026-10-06T10:00:00+00:00",
              source: str = "fx-kestrel", url: str = "", source_type: str = "press", tier: int = 2, **kw) -> Item:
    it = Item(id=id_, raw_id="raw-" + id_, source_id=source, source_type=source_type, family=kw.pop("family", "PRS"),
              tier=tier, source_url="", url=url, canonical_url=canonicalize(url)[0] if url else "",
              title=title, cleaned_content=content, summary=kw.pop("summary", ""), published_at=published, **kw)
    return it


# --------------------------------------------------------------------------- URLs

@pytest.mark.parametrize("raw, expected", [
    ("http://www.Example.example/News/Story/?utm_source=rss&utm_medium=feed&id=7#top", "https://example.example/News/Story?id=7"),
    ("https://example.example/a//b/index.html", "https://example.example/a/b"),
    ("https://m.example.example/story/", "https://example.example/story"),
    ("https://example.example/story/amp/", "https://example.example/story"),
    ("https://example.example:443/s?b=2&a=1&fbclid=x", "https://example.example/s?a=1&b=2"),
])
def test_canonicalize(raw, expected):
    assert canonicalize(raw)[0] == expected


def test_canonicalize_flags_and_bad_input():
    url, flags = canonicalize("http://www.example.example/x?utm_campaign=y#frag")
    assert "http_upgraded" in flags and "fragment_removed" in flags and "tracking_removed:1" in flags
    assert canonicalize("")[1] == ["empty_url"]
    assert canonicalize("not a url")[1] == ["not_absolute"]
    assert "aggregator_redirect" in canonicalize("https://news.google.example/rss/articles/X?oc=5")[1]
    assert canonicalize("https://news.google.example/rss/articles/X?oc=5")[0].endswith("/rss/articles/X")


# --------------------------------------------------------------------------- dates

@pytest.mark.parametrize("raw, iso", [
    ("Tue, 06 Oct 2026 09:00:00 +0200", "2026-10-06T07:00:00+00:00"),
    ("2026-10-06T21:50:00+00:00", "2026-10-06T21:50:00+00:00"),
    ("2026-10-06T21:50:00Z", "2026-10-06T21:50:00+00:00"),
    ("20261006T224500Z", "2026-10-06T22:45:00+00:00"),
    ("2026-10-05", "2026-10-05T00:00:00+00:00"),
    ("6 octobre 2026", "2026-10-06T00:00:00+00:00"),
    ("October 6, 2026", "2026-10-06T00:00:00+00:00"),
    ("06/10/2026", "2026-10-06T00:00:00+00:00"),
])
def test_parse_date_formats(raw, iso):
    dt, flags = parse_date(raw, collected_at=NOW)
    assert dt.isoformat() == iso
    assert "date_estimated" not in flags


@pytest.mark.parametrize("raw, flag", [("next Tuesday", "date_invalid"), ("2026-13-45", "date_invalid"), ("", "date_missing"),
                                      ("Mon, 15 Mar 2027 10:00:00 +0100", "date_future"), ("1970-01-01", "date_implausible")])
def test_parse_date_falls_back_with_flag(raw, flag):
    dt, flags = parse_date(raw, collected_at=NOW)
    assert dt == NOW and flag in flags and "date_estimated" in flags


# --------------------------------------------------------------------------- cleaning and language

def test_clean_text_removes_navigation_ads_and_boilerplate_keeps_text():
    html = ('<header><nav>Home | News</nav></header><div class="cookie-banner">We use cookies</div>'
            '<p>ESA selects Isar Aerospace.</p><script>var x=1</script><div class="ad-slot">Buy</div>'
            '<p>Second paragraph &amp; more.</p><p>Subscribe to our newsletter</p><footer>© 2026</footer>')
    text, flags = clean_text(html)
    assert text == "ESA selects Isar Aerospace.\n\nSecond paragraph & more."
    assert "boilerplate_removed" in flags


def test_clean_text_handles_malformed_html_and_empty():
    text, _ = clean_text("<p>Unclosed <b>bold <i>text</p><div>")
    assert "Unclosed bold text" in text
    assert clean_text("")[1] == ["empty_content"]
    assert clean_text("   ")[1] == ["empty_content"]


def test_mojibake_is_repaired():
    fixed, changed = fix_mojibake("Itâ€™s the DÃ©fense ministry")
    assert changed and fixed == "It’s the Défense ministry"
    assert fix_mojibake("Défense") == ("Défense", False)


def test_repeated_lines_detects_source_footers():
    texts = [f"Story {i}.\n\nESA is an intergovernmental organisation." for i in range(5)]
    assert repeated_lines(texts) == {"ESA is an intergovernmental organisation."}


def test_summary_prefers_feed_then_lead():
    assert make_summary("A feed summary long enough to be used as is, really.", "x")[1] == "feed"
    s, origin = make_summary("", "First sentence here. Second one. Third one.")
    assert origin == "extracted" and s == "First sentence here. Second one."


@pytest.mark.parametrize("text, lang", [
    ("The European Space Agency has selected the launch company for the missions of the challenge.", "en"),
    ("La Direction générale de l'armement a notifié une commande de systèmes pour les armées.", "fr"),
    ("Die Bundeswehr hat mit dem Unternehmen einen Vertrag für die Lieferung von Systemen geschlossen.", "de"),
    ("🚀🚀🚀 soon", "und"),
    ("Wojsko testuje nowe systemy antydronowe na poligonie", "und"),
])
def test_detect_language(text, lang):
    assert detect_language(text)[0] == lang


# --------------------------------------------------------------------------- similarity and dedup

def test_tokens_and_jaccard():
    assert tokens("The Ariane-6 launch, l'été") == ["ariane", "6", "launch", "ete"]
    assert jaccard({1, 2}, {2, 3}) == pytest.approx(1 / 3)
    tf = TfIdf({"a": "gallium export licensing radar", "b": "gallium export licensing radar", "c": "business jet deliveries"})
    assert tf.cosine("a", "b") == pytest.approx(1.0) and tf.cosine("a", "c") == 0.0


def test_proper_noun_swap():
    assert proper_noun_swap("Leonardo completes wind-tunnel campaign for Vesper", "ArianeGroup completes wind-tunnel campaign for Vesper")
    assert not proper_noun_swap("Replay: Ariane 6 launches two Galileo satellites", "Ariane 6 launches two Galileo satellites")


def test_dedup_levels_and_heads():
    long = "Gallium export licensing is stretching lead times for radar power amplifiers in Europe. " * 4
    items = [
        make_item("it-a", "Ariane 6 launches two Galileo satellites", url="https://kestrel.example/a6?utm_source=x"),
        make_item("it-b", "Ariane 6 launches two Galileo satellites", url="http://www.kestrel.example/a6/", published="2026-10-06T11:00:00+00:00"),
        make_item("it-c", "Ariane 6 launches two Galileo satellites", source="fx-gn-space", source_type="aggregator", family="AGG",
                  tier=3, published="2026-10-06T09:00:00+00:00", url="https://news.google.example/rss/articles/Z?oc=5"),
        make_item("it-d", "Gallium squeeze hits radar makers", content=long),
        make_item("it-e", "Radar makers feel the gallium squeeze", content=long, published="2026-10-06T12:00:00+00:00"),
        make_item("it-f", "ESA selects Isar Aerospace for 4 missions", published="2026-10-06T12:00:00+00:00"),
        make_item("it-g", "ESA selects Isar Aerospace for 6 missions", published="2026-10-06T13:00:00+00:00"),
    ]
    counts = deduplicate(items, CFG.scoring["dedup"])
    by = {i.id: i for i in items}
    assert by["it-b"].dedup_level == "L1_url"
    assert by["it-a"].duplicate_group_id == by["it-b"].duplicate_group_id == by["it-c"].duplicate_group_id
    # The aggregator copy came first but the publisher's own copy is the head.
    assert by["it-a"].is_origin_head and not by["it-c"].is_origin_head
    assert by["it-e"].duplicate_group_id == by["it-d"].duplicate_group_id and by["it-e"].dedup_level in ("L1_hash", "L3_content")
    # Different numbers: never merged by title (contradiction must stay visible).
    assert by["it-f"].duplicate_group_id != by["it-g"].duplicate_group_id
    assert counts["duplicates"] == 3


def test_dedup_respects_time_window():
    items = [make_item("it-1", "NATO tests counter-drone systems at exercise", published="2026-09-01T10:00:00+00:00"),
             make_item("it-2", "NATO tests counter-drone systems at exercise", published="2026-10-01T10:00:00+00:00")]
    deduplicate(items, CFG.scoring["dedup"])
    assert items[0].duplicate_group_id != items[1].duplicate_group_id


# --------------------------------------------------------------------------- entities

@pytest.mark.parametrize("surface", ["Airbus", "Airbus SE", "AIRBUS", "airbus", "Airbus Group"])
def test_entity_normalisation(surface):
    assert GAZ.resolve(surface).id == "airbus"


def test_entity_extraction_longest_match_and_case_rules():
    it = make_item("it-x", "French Ministry of the Armed Forces orders counter-drone systems from MBDA",
                   content="Airbus Defence and Space and esa la ESA selon les ses SES.")
    extract(it, GAZ)
    assert "fr-minarm" in it.organizations and "mbda" in it.companies and "counter-uas" in it.technologies
    assert "airbus-ds" in it.companies and "airbus" not in it.companies           # longest match wins
    assert it.organizations.count("esa") == 1 and "ses" in it.companies           # "esa"/"ses" lowercase ignored
    assert "FR" in it.countries                                                    # from the ministry's country


def test_publisher_entity_is_marked_as_provenance():
    it = make_item("it-y", "New integration hall opens")
    extract(it, GAZ, publisher_entity="thales")
    assert it.companies == ["thales"]
    assert it.mentions[0]["method"] == "publisher"


def test_entities_config_ids_are_unique_and_parents_exist():
    ids = [e["id"] for e in CFG.entities["entity"]]
    assert len(ids) == len(set(ids))
    for e in CFG.entities["entity"]:
        assert not e.get("parent") or e["parent"] in ids


# --------------------------------------------------------------------------- classification

def test_classification_multilabel_with_evidence():
    it = make_item("it-c1", "Commission assesses gallium supply risk for European defence electronics",
                   content="China tightened export licensing for gallium. Gallium nitride is used in radar power amplifiers. "
                           "Export restrictions now apply to shipments.")
    extract(it, GAZ)
    CLF.classify(it)
    assert {"DEFENSE"} <= set(it.sectors)
    assert {"SUPPLY_CHAIN", "SEMICONDUCTORS", "EXPORT_CONTROL"} <= set(it.topics)
    ev = it.classification["SUPPLY_CHAIN"]
    assert ev["entities"] == ["gallium"] and ev["score"] >= CLF.threshold
    assert it.processing_status == "processed"
    # One weak keyword is below the threshold: no label.
    weak = make_item("it-c1b", "Radar makers meet", content="Export licensing was mentioned once.")
    extract(weak, GAZ)
    CLF.classify(weak)
    assert "EXPORT_CONTROL" not in weak.topics


def test_classification_out_of_scope_and_event_type():
    bakery = make_item("it-c2", "Regional bakery chain opens its 40th store")
    extract(bakery, GAZ)
    CLF.classify(bakery)
    assert bakery.processing_status == "out_of_scope" and not bakery.sectors
    empty = make_item("it-c3", "Big week for European launchers.", content="Big week for European launchers.")
    extract(empty, GAZ)
    CLF.classify(empty)
    assert empty.processing_status == "out_of_scope" and "no_entity" in empty.flags
    order = make_item("it-c4", "MBDA wins French order for 40 counter-drone systems")
    extract(order, GAZ)
    CLF.classify(order)
    assert order.event_type == "contract" and "title:wins" in order.event_type_evidence


def test_new_category_is_config_only():
    tax = {**CFG.taxonomy, "category": {**CFG.taxonomy["category"],
                                        "SPACE_DEBRIS": {"kind": "topic", "keywords": ["debris", "deorbit*"], "entities": []}}}
    clf = Classifier(tax, GAZ)
    it = make_item("it-c5", "ESA deorbits satellite to reduce space debris")
    extract(it, GAZ)
    clf.classify(it)
    assert "SPACE_DEBRIS" in it.topics


# --------------------------------------------------------------------------- events

def test_event_conflicts_and_cross_language_rule():
    a = make_item("it-e1", "La DGA commande à MBDA 40 systèmes de lutte anti-drones", language="fr",
                  content="La DGA a notifié à MBDA une commande de systèmes de lutte anti-drones pour le ministère des Armées.")
    b = make_item("it-e2", "MBDA wins French order for 40 counter-drone systems", language="en",
                  content="The French defence procurement agency DGA ordered counter-drone systems from MBDA for the French Ministry of the Armed Forces.")
    for x in (a, b):
        extract(x, GAZ)
        CLF.classify(x)
    tf = TfIdf({x.id: x.text for x in (a, b)})
    sim, detail = pair_similarity(a, b, tf, CFG.scoring["events"])
    assert detail["cross_language"] and sim >= CFG.scoring["events"]["cross_language_entity_jaccard"]
    c = make_item("it-e3", "Saab wins Swedish order for counter-drone systems", content="Saab counter-drone order.")
    extract(c, GAZ)
    CLF.classify(c)
    assert "no company in common" in conflicts(b, c)


def test_numeric_facts_for_contradictions():
    it = make_item("it-n", "ESA selects Isar Aerospace for 4 missions", summary="The award covers €120 million and 4 launch missions.")
    facts = numeric_facts(it)
    assert {"unit": "missions", "value": 4.0} in facts
    assert {"unit": "amount_EUR_m", "value": 120.0} in facts


# --------------------------------------------------------------------------- scoring

def test_credibility_components_sum_and_penalty():
    value, expl = credibility(0.9, primary=True, origins=3, families=2, reliable_date_share=1.0, contradictions=1,
                              cfg=CFG.scoring, source_label="tier 1")
    comps = {c["key"]: c["value"] for c in expl["components"]}
    assert comps == {"source_reliability": 45.0, "primary_source_bonus": 15, "corroboration_bonus": 20,
                     "temporal_consistency": 10.0, "contradiction_penalty": -15}
    assert value == round(sum(comps.values())) == 75
    assert "contradiction_penalty" in expl["formula"]


def test_corroboration_is_capped_and_needs_independent_origins():
    _, one = credibility(0.75, primary=False, origins=1, families=1, reliable_date_share=1, contradictions=0, cfg=CFG.scoring, source_label="")
    _, many = credibility(0.75, primary=False, origins=30, families=5, reliable_date_share=1, contradictions=0, cfg=CFG.scoring, source_label="")
    corr = lambda e: next(c["value"] for c in e["components"] if c["key"] == "corroboration_bonus")  # noqa: E731
    assert corr(one) == 0 and corr(many) == CFG.scoring["credibility"]["corroboration_max"]


def test_importance_matches_brief_weights_and_neutral_recency_for_estimated_dates():
    w = CFG.scoring["importance"]
    assert (w["reliability"], w["origins"], w["relevance"], w["recency"], w["impact"]) == (0.15, 0.20, 0.20, 0.15, 0.30)
    v1, e1 = importance(best_baseline=0.9, origins=1, relevance_value=60, first_seen=NOW.isoformat(), as_of=NOW, impact=50, cfg=CFG.scoring)
    v2, e2 = importance(best_baseline=0.9, origins=1, relevance_value=60, first_seen=NOW.isoformat(), as_of=NOW, impact=50,
                        cfg=CFG.scoring, date_reliable=False)
    rec = lambda e: next(c for c in e["components"] if c["key"] == "recency")  # noqa: E731
    assert rec(e1)["value"] == 100 and rec(e2)["value"] == 50 and v2 < v1
    assert v1 == round(sum(c["contribution"] for c in e1["components"]))


def test_data_quality_is_separate_from_credibility():
    good = make_item("it-q1", "Full record", content="x" * 500, author="Staff", url="https://a.example/x", language="en")
    good.companies = ["airbus"]
    good.sectors = ["AERONAUTICS"]
    poor = make_item("it-q2", "", url="", language="und", flags=["date_estimated", "title_derived", "metadata_only"])
    assert data_quality(good, CFG.scoring)[0] == 100
    assert data_quality(poor, CFG.scoring)[0] < 30


# --------------------------------------------------------------------------- statistics

def test_series_stats_acceleration_and_floor():
    st = series_stats([3, 2, 3, 3, 4, 3, 2, 3, 3, 4, 3, 3, 9])
    assert st.current == 9 and st.baseline_mean == pytest.approx(3.0)
    assert st.z == pytest.approx(6 / 3 ** 0.5, rel=1e-2)     # std floored at sqrt(mean) (Poisson)
    assert st.growth == pytest.approx(2.0) and st.p_high < 0.01
    sparse = series_stats([0] * 12 + [1])
    assert sparse.z == 2.0                                     # floor 0.5: one event cannot make z explode


def test_poisson_cdf():
    assert poisson_cdf(0, 3.0) == pytest.approx(0.0498, abs=1e-4)
    assert poisson_cdf(10, 0) == 1.0
