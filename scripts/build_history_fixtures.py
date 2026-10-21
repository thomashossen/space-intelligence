"""Generate the 13-week FICTITIOUS history replayed by the fixture pipeline run.

    python scripts/build_history_fixtures.py

Deterministic (fixed seed, fixed as-of 2026-10-07 06:30 UTC). Writes RSS files to
data/fixtures/feeds/history/ and a ground-truth manifest
(data/fixtures/history_manifest.json) listing which items report the same story,
so tests can MEASURE deduplication and clustering quality instead of asserting it.

Every story is invented. Actor and programme names are real;
nothing described happened. Hosts use the reserved .example TLD.

Weekly story counts are fixed per theme so the signals are known in advance:
  counter-UAS   ~3/week → 6 more in the current week (+ today's fixtures)  → acceleration
  hypersonic    ~2/week → 5 more in the current week                        → acceleration
  launchers     ~4/week → 2 more in the current week (+ 4 today)            → stable, no signal
  business jets ~3.5/week → 0 in the current week                            → drop
  gallium       ~0.25/week → 2 in the current week (+ today)                 → surge
  satellites, Airbus commercial, institutions (background coverage): stable
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "fixtures" / "feeds" / "history"
MANIFEST = ROOT / "data" / "fixtures" / "history_manifest.json"
AS_OF = datetime(2026, 10, 7, 6, 30, tzinfo=timezone.utc)
SEED = 20261007

SOURCES = {
    "fx-esa": ("ESA newsroom", "www.esa.example"),
    "fx-mbda": ("MBDA newsroom", "www.mbda.example"),
    "fx-airbus": ("Airbus newsroom", "www.airbus.example"),
    "fx-kestrel": ("Kestrel Space Wire", "kestrel-spacewire.example"),
    "fx-northline": ("Northline Defence Monitor", "northline-defence.example"),
    "fx-halden": ("Halden Industry Bulletin", "halden-bulletin.example"),
    "fx-brightwater": ("Brightwater Aviation Weekly", "brightwater-aviation.example"),
    "fx-gn-space": ("Google News", "news.google.example"),
}
ESA_FOOTER = ("ESA is an intergovernmental organisation with member states across Europe. "
              "Media enquiries: media-desk@esa.example")

COUNTRIES = [("Polish", "Poland"), ("Dutch", "the Netherlands"), ("Belgian", "Belgium"), ("Finnish", "Finland"),
             ("Danish", "Denmark"), ("Norwegian", "Norway"), ("Spanish", "Spain"), ("Italian", "Italy"),
             ("Swedish", "Sweden"), ("German", "Germany")]
MONTHS = ["July", "August", "September", "October", "November", "December"]

THEMES: dict[str, dict] = {
    "counter_uas": {
        "baseline": [3, 2, 3, 3, 4, 3, 2, 3, 3, 4, 3, 3], "current": 6,
        "primary": ["fx-northline"], "secondary": ["fx-halden"], "gn": 0.3,
        "actors": ["Rheinmetall", "Saab", "Kongsberg", "Hensoldt", "Diehl Defence", "Quantum Systems", "Helsing", "Leonardo", "Indra"],
        "systems": ["Skyranger", "Nimbus", "Aegir", "Kestrel-Net", "Falx", "Harrier-C", "Brontes", "Vigil", "Corax", "Tessera", "Lumen", "Arbalest"],
        "titles": [
            ("contract", "{actor} wins {adj} order for {n} {system} counter-drone systems"),
            ("test", "{actor} tests {system} counter-UAS interceptor against drone swarms in {country}"),
            ("delivery", "{actor} delivers first {system} anti-drone units to {adj} forces"),
        ],
        "alt_titles": [
            "{adj} forces receive {system} counter-drone capability from {actor}",
            "{system}: {actor} and {adj} army move ahead on drone defence",
        ],
        "body": [
            "The {system} counter-UAS system pairs a {sensor} with {effector} to defeat small drones.",
            "{actor} said the {n} units would be fielded with {adj} units from {month} {year}.",
            "The programme reference {ref} covers training and spare parts.",
        ],
    },
    "hypersonic": {
        "baseline": [2, 1, 2, 2, 1, 2, 3, 2, 2, 1, 2, 2], "current": 5,
        "primary": ["fx-northline", "fx-mbda"], "secondary": ["fx-halden"], "gn": 0.0,
        "actors": ["MBDA", "Lockheed Martin", "Northrop Grumman", "Raytheon", "Leonardo", "ArianeGroup", "Diehl Defence"],
        "systems": ["Aquila", "Stratos", "Helix-H", "Vesper", "Orion-G", "Talon-X", "Meridian", "Sirocco", "Boreas"],
        "titles": [
            ("test", "{actor} completes wind-tunnel campaign for {system} hypersonic glide vehicle"),
            ("contract", "{adj} ministry funds {system} hypersonic interceptor study with {actor}"),
            ("research", "{actor} unveils {system} hypersonic cruise missile concept"),
        ],
        "alt_titles": [
            "{system} hypersonic effort: {actor} reports progress to {adj} officials",
            "Inside {actor}'s {system} hypersonic programme",
        ],
        "body": [
            "The {system} work focuses on {topic} for hypersonic flight above Mach 5.",
            "{actor} said test results will feed a {adj} decision expected in {month} {year}.",
            "The study, reference {ref}, runs for {n} months.",
        ],
    },
    "launchers": {
        "baseline": [4, 4, 3, 5, 4, 4, 4, 3, 4, 5, 4, 4], "current": 2,
        "primary": ["fx-kestrel"], "secondary": ["fx-halden"], "gn": 0.3,
        "pairs": [("Isar Aerospace", "Spectrum"), ("Rocket Factory Augsburg", "RFA One"), ("Avio", "Vega C"),
                  ("Rocket Lab", "Neutron"), ("The Exploration Company", "Nyx"), ("ArianeGroup", "Ariane 6")],
        "titles": [
            ("test", "{actor} completes {n}-second engine test for {system}"),
            ("schedule_change", "{actor} sets {month} window for next {system} flight"),
            ("launch", "{system} launch by {actor} places {n} small satellites in orbit"),
            ("investment", "{actor} expands {system} production line in {country}"),
        ],
        "alt_titles": [
            "{system}: {actor} reports {n}-step progress toward next mission",
            "Launcher update: {actor} and the {system} campaign",
        ],
        "body": [
            "{actor} said the {system} campaign reached milestone {ref}.",
            "The company plans {n} flights of {system} from {month} {year}.",
            "Engineers worked on {topic} during the campaign.",
        ],
    },
    "business_jets": {
        "baseline": [3, 4, 3, 4, 3, 4, 4, 3, 4, 3, 4, 3], "current": 0,
        "primary": ["fx-brightwater"], "secondary": ["fx-halden"], "gn": 0.0,
        "pairs": [("Gulfstream", "G700"), ("Gulfstream", "G800"), ("Bombardier", "Global 8000"), ("Bombardier", "Global 7500"),
                  ("Dassault Aviation", "Falcon 10X"), ("Dassault Aviation", "Falcon 6X"), ("Textron Aviation", "Citation Longitude"),
                  ("Embraer", "Praetor 600")],
        "titles": [
            ("delivery", "{actor} delivers {n} {system} business jets in {month}"),
            ("test", "{system} completes certification flight test milestone for {actor}"),
            ("investment", "{actor} raises {system} production as business aviation demand holds"),
        ],
        "alt_titles": [
            "Business jet market: {actor} {system} numbers in focus",
            "{actor} {system} update for business aviation customers",
        ],
        "body": [
            "{actor} reported {n} {system} business jets handed over to customers.",
            "The large-cabin business jet segment remains supported by fleet renewal, the company said in {month} {year}.",
            "Order reference {ref} was disclosed with the update.",
        ],
    },
    "gallium": {
        "baseline": [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0], "current": 2,
        "primary": ["fx-halden"], "secondary": ["fx-northline"], "gn": 0.3,
        "actors": ["Hensoldt", "Thales", "Saab", "Leonardo", "Indra"],
        "systems": ["GaN radar chips", "power amplifiers", "AESA modules"],
        "titles": [
            ("other", "{adj} radar makers report longer lead times for gallium nitride chips"),
            ("export_control", "Gallium export licensing slows shipments to {adj} defence suppliers"),
        ],
        "alt_titles": ["Gallium supply: {actor} reviews {system} inventories"],
        "body": [
            "{actor} said stocks of {system} cover about {n} weeks of production.",
            "Suppliers cite export licensing for gallium and a supply chain bottleneck for {topic}.",
            "The issue was raised at an industry meeting in {month} {year} (ref. {ref}).",
        ],
    },
    "satellites": {
        "baseline": [4, 4, 4, 3, 4, 5, 4, 4, 3, 4, 4, 4], "current": 3,
        "primary": ["fx-kestrel", "fx-esa"], "secondary": ["fx-halden"], "gn": 0.0,
        "actors": ["OHB", "Thales Alenia Space", "Airbus Defence and Space", "Eutelsat", "SES"],
        "systems": ["Galileo", "IRIS²", "Copernicus", "Sentinel", "Galileo Second Generation"],
        "titles": [
            ("contract", "{actor} wins contract for {n} {system} satellites"),
            ("delivery", "{actor} ships {system} satellite to the launch site"),
            ("test", "{system} satellite built by {actor} passes thermal vacuum tests"),
        ],
        "alt_titles": ["{system}: {actor} satellite milestone confirmed", "{actor} and the {system} satellite schedule"],
        "body": [
            "The {system} satellite built by {actor} carries a {topic} payload.",
            "{actor} expects {n} spacecraft to be ready by {month} {year}.",
            "Milestone {ref} was reached on schedule.",
        ],
    },
    "airbus": {
        "baseline": [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4], "current": 3,
        "primary": ["fx-airbus"], "secondary": ["fx-brightwater"], "gn": 0.0,
        "actors": ["Airbus"],
        "systems": ["A320neo", "A321XLR", "A350", "A350F"],
        "airlines": ["Northwind Airways", "Celadon Air", "Meridia Airlines", "Alder Air", "Solano Airlines", "Brisa Air", "Kestrel Airways", "Lumo Air"],
        "titles": [
            ("contract", "{airline} orders {n} {system} aircraft from Airbus"),
            ("delivery", "Airbus delivers first {system} to {airline}"),
            ("investment", "Airbus adds {system} production capacity in {country}"),
        ],
        "alt_titles": ["{airline} and Airbus: {n} {system} deal details", "Airbus {system} order from {airline} confirmed"],
        "body": [
            "{airline} will operate the {system} on routes from its main base.",
            "Airbus said the deal for {n} aircraft was finalised in {month} {year}.",
            "The agreement, reference {ref}, includes options.",
        ],
    },
    # Background coverage: the institutions and countries that appear every week in real A&D news.
    "institutions": {
        "baseline": [3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 3], "current": 3,
        "primary": ["fx-northline", "fx-kestrel", "fx-esa"], "secondary": ["fx-halden"], "gn": 0.0,
        "pairs": [
            ("NATO", "air defence"), ("European Commission", "defence readiness"), ("French Ministry of the Armed Forces", "budget"),
            ("DGA", "Rafale upgrade"), ("Arianespace", "Ariane 6"), ("ESA", "Galileo"), ("Thales", "radar"),
            ("European Defence Fund", "European Defence Fund"), ("EUSPA", "Galileo"), ("Bundeswehr", "air defence"),
            ("NATO", "deterrence"), ("European Commission", "space programme"),
        ],
        "titles": [
            ("other", "{actor} sets out {system} priorities for {year}"),
            ("regulation", "{actor} publishes {system} work programme with {n} actions"),
            ("other", "{actor} officials discuss {system} with French and German counterparts"),
            ("other", "{actor} reviews {system} amid tensions with China and Russia"),
        ],
        "alt_titles": ["{actor}: what the {system} update means", "{system}: {actor} lays out next steps"],
        "body": [
            "{actor} presented {system} priorities covering {n} measures.",
            "Officials from France and Germany attended the {month} meeting.",
            "Document {ref} will be discussed again in {year}.",
        ],
    },
}
ALT_BODY = ("{alt}. {actor} confirmed the {system} {kind} this week, {adj} officials said. "
            "The {n} figure was quoted by {actor}. Reference {ref}.")
KIND = {"counter_uas": "counter-drone programme", "hypersonic": "hypersonic project", "launchers": "launch campaign",
        "business_jets": "business jet programme", "gallium": "gallium supply issue", "satellites": "satellite programme",
        "airbus": "aircraft deal", "institutions": "policy update"}
TOPICS_BY_THEME = {
    "launchers": ["the rocket engine hot-fire sequence", "reusable first stage recovery", "upper stage integration", "launch pad readiness"],
}
SENSORS = ["passive RF detector", "3D radar", "electro-optical turret", "acoustic array", "multi-band jammer"]
EFFECTORS = ["a 35 mm gun", "interceptor drones", "a laser effector", "guided rockets", "a net launcher"]
TOPICS = ["thermal protection", "guidance", "materials", "propulsion", "ground testing", "avionics", "optical", "radar", "telecommunications"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:70]


def story_time(rng: random.Random, week: int) -> datetime:
    """Week 0 = current week (between 1 and 6.5 days before as-of)."""
    if week == 0:
        return AS_OF - timedelta(hours=rng.uniform(26, 156))
    return AS_OF - timedelta(days=7 * week) - timedelta(hours=rng.uniform(2, 166))


def build() -> dict:
    rng = random.Random(SEED)
    feeds: dict[str, list[dict]] = {sid: [] for sid in SOURCES}
    stories = []
    counter = 0
    last_seen: dict[tuple, datetime] = {}
    for theme, spec in THEMES.items():
        plan = [(12 - i, n) for i, n in enumerate(spec["baseline"])] + [(0, spec["current"])]
        for week, n_stories in plan:
            for _ in range(n_stories):
                counter += 1
                sid = f"h{counter:04d}"
                adj, country = rng.choice(COUNTRIES)
                t0 = story_time(rng, week)
                # Ground truth must be unambiguous: the same actor + system is not reused within 5 days.
                for _ in range(20):
                    actor, system = rng.choice(spec["pairs"]) if "pairs" in spec else (rng.choice(spec["actors"]), rng.choice(spec["systems"]))
                    airline = rng.choice(spec.get("airlines", ["—"]))
                    prev = last_seen.get((theme, actor, system)) or last_seen.get((theme, airline))
                    if prev is None or abs((t0 - prev).total_seconds()) > 5 * 86400:
                        break
                last_seen[(theme, actor, system)] = t0
                if "airlines" in spec:
                    last_seen[(theme, airline)] = t0
                slots = {
                    "actor": actor, "system": system,
                    "adj": adj, "country": country, "n": rng.randint(2, 48),
                    "month": rng.choice(MONTHS), "year": rng.choice([2027, 2028]),
                    "ref": f"{theme[:3].upper()}-{rng.randint(100, 999)}", "sensor": rng.choice(SENSORS),
                    "effector": rng.choice(EFFECTORS), "topic": rng.choice(TOPICS_BY_THEME.get(theme, TOPICS)),
                    "airline": airline,
                }
                etype, title_t = rng.choice(spec["titles"])
                title = title_t.format(**slots)
                body = " ".join(s.format(**slots) for s in spec["body"])
                primary = rng.choice(spec["primary"])
                if primary == "fx-mbda" and slots["actor"] != "MBDA":
                    primary = "fx-northline"
                members = [(primary, title, body, t0, "origin")]
                if rng.random() < 0.4:
                    alt = rng.choice(spec["alt_titles"]).format(**slots)
                    sec = rng.choice([s for s in spec["secondary"] if s != primary] or spec["secondary"])
                    alt_body = ALT_BODY.format(alt=alt, kind=KIND[theme], **slots)
                    members.append((sec, alt, alt_body, t0 + timedelta(hours=rng.uniform(2, 30)), "origin"))
                if spec["gn"] and rng.random() < spec["gn"]:
                    members.append(("fx-gn-space", title, "", t0 + timedelta(hours=rng.uniform(1, 6)), "copy_of_first"))
                guids = []
                for k, (src, ttl, txt, t, role) in enumerate(members):
                    t = min(t, AS_OF - timedelta(hours=25))
                    guid = f"hist-{sid}-{k}"
                    guids.append(guid)
                    feeds[src].append({"guid": guid, "title": ttl, "text": txt, "time": t, "story": sid,
                                       "publisher": SOURCES[members[0][0]][0] if src == "fx-gn-space" else "",
                                       "publisher_domain": SOURCES[members[0][0]][1]})
                stories.append({"story": sid, "theme": theme, "week": week, "event_type": etype,
                                "items": guids, "roles": [m[4] for m in members],
                                "origins": sum(1 for m in members if m[4] == "origin")})
    for sid, entries in feeds.items():
        if entries:
            write_feed(sid, entries)
    manifest = {
        "notice": "FICTITIOUS history generated by scripts/build_history_fixtures.py. Nothing here happened.",
        "as_of": AS_OF.isoformat(), "seed": SEED,
        "themes": {t: {"baseline": s["baseline"], "current_history": s["current"]} for t, s in THEMES.items()},
        "stories": stories,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def write_feed(source_id: str, entries: list[dict]) -> None:
    name, domain = SOURCES[source_id]
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">', "<channel>",
             f"<title>{escape(name)} — history (FICTITIOUS)</title>", f"<link>https://{domain}/</link>",
             "<description>Generated fixture history. Every item is invented.</description>", "<language>en</language>"]
    for e in sorted(entries, key=lambda x: x["time"], reverse=True):
        when = format_datetime(e["time"])
        if source_id == "fx-gn-space":
            token = hashlib.sha1(e["guid"].encode()).hexdigest()[:24]
            link = f"https://news.google.example/rss/articles/CBMi{token}?oc=5"
            parts += ["<item>", f"<title>{escape(e['title'])} - {escape(e['publisher'])}</title>", f"<link>{link}</link>",
                      f"<guid isPermaLink=\"false\">{e['guid']}</guid>", f"<pubDate>{when}</pubDate>",
                      f"<description>{escape('<a href=\"' + link + '\">' + e['title'] + '</a>')}</description>",
                      f"<source url=\"https://{e['publisher_domain']}\">{escape(e['publisher'])}</source>", "</item>"]
            continue
        link = f"https://{domain}/{e['time']:%Y/%m}/{slug(e['title'])}-{e['story']}"
        lead = e["text"].split(". ")[0] + "."
        html = "".join(f"<p>{escape(s.strip())}{'' if s.strip().endswith('.') else '.'}</p>" for s in e["text"].split(". ") if s.strip())
        if source_id == "fx-esa":
            html += f"<p>{escape(ESA_FOOTER)}</p>"
        html = '<nav class="site-nav"><a href="/">Home</a> | <a href="/news">News</a></nav>' + html + \
               '<div class="share-buttons">Share this article</div>'
        parts += ["<item>", f"<title>{escape(e['title'])}</title>", f"<link>{link}</link>",
                  f"<guid isPermaLink=\"false\">{e['guid']}</guid>", f"<pubDate>{when}</pubDate>",
                  f"<description>{escape(lead)}</description>",
                  f"<content:encoded><![CDATA[{html}]]></content:encoded>", "</item>"]
    parts += ["</channel>", "</rss>", ""]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{source_id}.xml").write_text("\n".join(parts), encoding="utf-8")


if __name__ == "__main__":
    m = build()
    n_items = sum(len(s["items"]) for s in m["stories"])
    print(f"{len(m['stories'])} stories, {n_items} items → {OUT.relative_to(ROOT)}")
