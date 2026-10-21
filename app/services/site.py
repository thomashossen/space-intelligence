"""View models for the public site (home, news, event, trends, actors).

Reads the pipeline database through ``Reader`` and turns rows into French,
display-ready dictionaries. No business logic is added here: scores, statuses
and counts come from the pipeline as they are.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.config import TZ, pipeline_db
from app.pipeline.queries import DatabaseMissing, Page, Reader

MONTHS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")

LABELS = {
    "SPACE": "Espace", "DEFENSE": "Défense", "AERONAUTICS": "Aéronautique", "CYBER": "Cyber", "DRONES": "Drones",
    "MISSILES": "Missiles", "SATELLITES": "Satellites", "LAUNCHERS": "Lanceurs", "PROPULSION": "Propulsion",
    "AI": "IA", "QUANTUM": "Quantique", "SEMICONDUCTORS": "Semi-conducteurs", "SUPPLY_CHAIN": "Approvisionnement",
    "ENERGY": "Énergie", "REGULATION": "Réglementation", "GEOPOLITICS": "Géopolitique",
    "EXPORT_CONTROL": "Contrôle export", "M&A": "Fusions-acquisitions", "CAPEX": "Investissement industriel",
    "R&D": "R&D",
}

# event_type -> (label, colour key, icon)
EVENT_TYPES = {
    "launch": ("Lancement", "launch", "rocket"),
    "contract": ("Contrat", "contract", "contract"),
    "test": ("Essai", "test", "target"),
    "delivery": ("Livraison", "delivery", "box"),
    "partnership": ("Partenariat", "partner", "link"),
    "acquisition": ("Acquisition", "invest", "coins"),
    "investment": ("Investissement", "invest", "coins"),
    "regulation": ("Réglementation", "regul", "scale"),
    "export_control": ("Contrôle export", "regul", "shield"),
    "schedule_change": ("Report", "delay", "clock"),
    "research": ("Recherche", "research", "atom"),
    "other": ("Actualité", "other", "satellite"),
}

STATUS = {
    "confirmed": "Confirmé",
    "candidate": "Source unique",
    "contested": "Contesté",
}

SCOPES = {"": "Tout", "space": "Espace", "defense": "Défense"}

ENTITY_TYPES = (
    ("company", "Entreprises", "building"),
    ("organization", "Institutions", "landmark"),
    ("programme", "Programmes", "orbit"),
    ("technology", "Technologies", "chip"),
    ("country", "Pays", "globe"),
)


def open_reader() -> Reader | None:
    try:
        return Reader(pipeline_db())
    except DatabaseMissing:
        return None


# ---------------------------------------------------------------------- formatting
def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(TZ)
    except ValueError:
        return None


def fr_date(value: str | None, with_time: bool = False) -> str:
    d = _dt(value)
    if d is None:
        return ""
    out = f"{d.day} {MONTHS[d.month - 1]} {d.year}"
    return f"{out} à {d:%H:%M}" if with_time else out


def label(code: str) -> str:
    return LABELS.get(code, code.replace("_", " ").capitalize())


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def event_card(e: dict) -> dict:
    name, colour, icon = EVENT_TYPES.get(e["event_type"], EVENT_TYPES["other"])
    actors = [x["name"] for x in e["companies"] + e["organizations"] + e["programmes"]][:3]
    tags = [label(s) for s in e["sectors"]] + actors
    brief = e.get("brief") or None
    return {
        "id": e["id"], "brief": bool(brief),
        "title": brief["titre"] if brief else e["title"],
        "original_title": e["title"],
        "summary": _clip(brief["resume"] if brief else e["description"], 240),
        "families": e["proof"]["families"], "lead_source": (e["sources"] or [""])[0],
        "event_type": e["event_type"],
        "type": name, "colour": colour, "icon": icon,
        "date": fr_date(e["first_seen"]), "datetime": e["first_seen"],
        "tags": tags[:4],
        "status": STATUS.get(e["status"], e["status"]), "status_key": e["status"],
        "origins": e["proof"]["orig"], "articles": e["proof"]["src"],
        "importance": e["scores"]["importance"],
    }


def item_card(it: dict) -> dict:
    return {
        "title": it["title"], "url": it["url"], "summary": _clip(it["summary"], 160),
        "date": fr_date(it["published_at"]), "source": it["source"]["name"],
    }


# ---------------------------------------------------------------------- shared context
def collection(reader: Reader) -> dict:
    st = reader.status()
    run = st["last_run"]
    return {
        "origin": st["data_origin"],
        "fixture": st["data_origin"] == "fixture",
        "as_of": run["as_of"] if run else None,
        "last_run": fr_date(run["finished_at"] or run["started_at"], with_time=True) if run else "",
        "run_status": run["status"] if run else None,
        "sources_ok": st["sources"]["ok"] + st["sources"]["warning"],
        "sources_active": st["sources"]["active"],
        "sources_error": st["sources"]["in_error"],
        "articles": st["counts"]["articles"],
        "events": st["counts"]["events"],
        "entities": st["counts"]["entities_seen"],
        "signals": st["counts"]["signals"],
        "history_weeks": (run["metrics"] or {}).get("history_weeks", 0) if run else 0,
    }


def _since(info: dict, days: int) -> str | None:
    d = _dt(info["as_of"])
    return (d - timedelta(days=days)).isoformat() if d else None


# ---------------------------------------------------------------------- pages
def trend_rows(reader: Reader, limit: int | None = None) -> list[dict]:
    rows = []
    for t in reader.topics()["data"]:
        cur, base = t["events_last_7d"], t["baseline_weekly_mean"]
        if cur == 0 and base == 0:
            continue
        if t["signal"] in ("acceleration", "surge"):
            mood, word = "up", "Signal de hausse"
        elif t["signal"] == "drop":
            mood, word = "down", "Signal de baisse"
        elif cur >= 2 * base + 1:
            mood, word = "up", "En forte hausse"
        elif cur > base * 1.2 + 0.5:
            mood, word = "up", "En hausse"
        elif cur < base * 0.5:
            mood, word = "down", "En baisse"
        else:
            mood, word = "flat", "Stable"
        short = {"Signal de hausse": "Signal ↑", "Signal de baisse": "Signal ↓", "En forte hausse": "Forte hausse",
                 "En hausse": "Hausse", "En baisse": "Baisse"}.get(word, word)
        rows.append({"code": t["label"], "label": label(t["label"]), "kind": t["kind"], "current": cur, "short": short,
                     "baseline": base, "mood": mood, "word": word, "signal": bool(t["signal"])})
    rows.sort(key=lambda r: (r["mood"] != "up", -r["current"]))
    return rows[:limit] if limit else rows


def pick_front_page(cards: list[dict], n: int = 5) -> list[dict]:
    """Most important first, with variety: at most 2 events known only through aggregators,
    1 research paper (they have their own column) and 2 led by the same source."""
    out, agg_only, research, per_source = [], 0, 0, {}
    for c in cards:
        only_agg = set(c["families"]) <= {"AGG"}
        if (only_agg and agg_only >= 2) or (c["event_type"] == "research" and research >= 1) \
                or per_source.get(c["lead_source"], 0) >= 2:
            continue
        out.append(c)
        agg_only += only_agg
        research += c["event_type"] == "research"
        per_source[c["lead_source"]] = per_source.get(c["lead_source"], 0) + 1
        if len(out) == n:
            break
    return out


def home(reader: Reader) -> dict:
    info = collection(reader)
    une, window = [], 7
    for days in (7, 30, 120):
        res = reader.events(since=_since(info, days), sort="importance", page=Page.of(1, 40))
        une, window = pick_front_page([event_card(e) for e in res["data"]]), days
        if len(une) >= 4:
            break
    research = reader.items(event_type="research", sort="published", page=Page.of(1, 4))["data"]
    ents = {t: reader.entities(type=t, min_events=1, page=Page.of(1, 6)) for t, _, _ in ENTITY_TYPES}
    top_actors = sorted(ents["company"]["data"] + ents["organization"]["data"], key=lambda e: -e["event_count"])[:6]
    return {
        "info": info,
        "une": une, "une_window": window,
        "research": [item_card(i) for i in research],
        "trends": trend_rows(reader, 4),
        "ecosystem": [{"type": t, "label": lab, "icon": icon, "count": ents[t]["total"]} for t, lab, icon in ENTITY_TYPES],
        "suggestions": [{"id": a["id"], "name": actor_name(a)} for a in top_actors[:5]],
    }


def news(reader: Reader, *, q: str, scope: str, kind: str, theme: str, sort: str, page: int) -> dict:
    info = collection(reader)
    res = reader.events(q=q or None, sector=scope or None, event_type=kind or None, topic=theme or None,
                        sort="importance" if sort == "importance" else "recent", page=Page.of(page, 20))
    return {"info": info, "events": [event_card(e) for e in res["data"]], "total": res["total"],
            "page": res["page"], "pages": res["pages"], "theme_label": label(theme) if theme else "",
            "types": [(k, v[0]) for k, v in EVENT_TYPES.items()], "scopes": list(SCOPES.items())}


STRATEGIC = {
    "geopolitical": ("Géopolitique", "L'événement touche aux relations entre États, aux alliances ou aux rapports de force."),
    "industrial": ("Industrie", "Il concerne des commandes, des capacités de production ou la position d'industriels."),
    "technological": ("Technologie", "Il implique des technologies critiques ou émergentes."),
    "supply_chain": ("Approvisionnement", "Il porte sur des matières, des composants ou des fournisseurs critiques."),
    "sovereignty": ("Souveraineté", "Il pèse sur l'autonomie stratégique, européenne ou nationale."),
}


def _names(reader: Reader, note: str) -> list[str]:
    tail = note.split(":", 1)[1] if ":" in note else ""
    return [" et ".join(reader.names.get(x.strip(), x.strip()) for x in part.split("×")) for part in tail.split(";") if part.strip()]


def score_reading(reader: Reader, expl: dict) -> list[dict]:
    """French reading of the pipeline's own score components (no language model)."""
    rows = []
    comps = sorted(expl.get("strategic", {}).get("components", []), key=lambda c: -c.get("value", 0))
    for c in comps:
        if c.get("value", 0) >= 40 and c["key"] in STRATEGIC and len(rows) < 3:
            name, text = STRATEGIC[c["key"]]
            rows.append({"label": name, "text": text, "value": c["value"]})
    for c in expl.get("novelty", {}).get("components", []):
        if c.get("value", 0) <= 0:
            continue
        if c["key"] == "new_actor":
            rows.append({"label": "Nouvel acteur", "text": "Première apparition récente dans l'actualité suivie : "
                         + ", ".join(_names(reader, c["note"])[:3]) + ".", "value": None})
        elif c["key"] == "new_relation":
            rows.append({"label": "Nouvelle relation", "text": "Première association observée entre "
                         + " ; ".join(_names(reader, c["note"])[:2]) + ".", "value": None})
    return rows


def _stake(text: str) -> dict:
    """« Libellé : explication » → label and text; anything else is kept whole."""
    head, sep, tail = text.partition(":")
    if sep and 0 < len(head.strip()) <= 40 and tail.strip():
        tail = tail.strip()
        return {"label": head.strip(), "text": tail[:1].upper() + tail[1:]}
    return {"label": "", "text": text}


def event(reader: Reader, event_id: str) -> dict | None:
    e = reader.event(event_id)
    if e is None:
        return None
    card = event_card(e)
    sources = []
    for o in e["origins"]:
        h = o["head"]
        if not h:
            continue
        sources.append({"title": h["title"], "url": h["url"], "source": h["source"], "publisher": h["publisher"],
                        "date": fr_date(h["published_at"], with_time=True), "primary": h["primary"],
                        "copies": len(o["copies"])})
    from app.services.explain import Explainer  # noqa: PLC0415

    scores = Explainer(reader.names, LABELS, COUNTRIES, {k: v[0] for k, v in EVENT_TYPES.items()}).sections(e)
    if e["status"] == "contested":
        status_text = "Les sources ne donnent pas les mêmes chiffres : l'information est contestée."
    elif e["status"] == "confirmed":
        status_text = f"Rapporté par {e['proof']['orig']} origines indépendantes, issues de {e['proof']['fam']} familles de sources."
    else:
        status_text = "Une seule origine pour l'instant : l'information n'est pas encore recoupée."
    actors = [{"name": x["name"], "id": x["id"]} for x in e["companies"] + e["organizations"] + e["programmes"] + e["technologies"]]
    brief = e.get("brief")
    if brief:
        brief = dict(brief, date=fr_date(brief["created_at"], with_time=True),
                     enjeux=[_stake(x) for x in brief["enjeux"]])
    return {"info": collection(reader), "e": card, "description": e["description"], "sources": sources,
            "brief": brief, "reading": [] if brief and brief.get("enjeux") else score_reading(reader, e.get("explanation", {})),
            "scores": scores, "status_text": status_text, "actors": actors,
            "topics": [label(t) for t in e["sectors"] + e["topics"]], "countries": e["countries"],
            "event_date": fr_date(e["event_date"]) if e.get("event_date") else card["date"]}


CONFIDENCE = {"high": "élevée", "medium": "moyenne", "low": "faible"}


def fr_num(x) -> str:
    if x is None:
        return "–"
    x = round(float(x), 1)
    return (str(int(x)) if x == int(x) else f"{x:.1f}").replace(".", ",")


SIGNAL_KINDS = {"acceleration": "Accélération", "surge": "Apparition", "drop": "Recul",
                "new_entity": "Nouvel acteur", "new_relation": "Nouvelle relation"}


def signal_card(s: dict) -> dict:
    st = s["stats"]
    subject = s["subject"]["label"] or s["subject"]["id"]
    sid, stype = s["subject"]["id"], s["subject"]["type"]
    href = (f"/actualites?theme={sid}" if sid in LABELS else
            (f"/acteurs/{sid}" if stype in ("entity", "company", "organization", "programme", "technology", "country", "region")
             or sid.startswith(("country-", "region-")) else ""))
    return {"kind": SIGNAL_KINDS.get(s["kind"], s["kind"]), "up": s["kind"] != "drop", "href": href,
            "subject": label(subject) if subject in LABELS else (country_name(subject) if subject.upper() in COUNTRIES else subject),
            "current": fr_num(st["current_week"]), "baseline": fr_num(st["baseline_weekly_mean"]),
            "confidence": CONFIDENCE.get(s["confidence"]["level"], s["confidence"]["level"]), "evidence": [e for e in s["evidence"] if e["title"]][:3]}


THEME_ICONS = {"SPACE": "orbit", "DEFENSE": "shield", "AERONAUTICS": "rocket", "CYBER": "chip", "DRONES": "radar",
               "MISSILES": "target", "SATELLITES": "satellite", "LAUNCHERS": "rocket", "PROPULSION": "rocket",
               "AI": "chip", "QUANTUM": "atom", "SEMICONDUCTORS": "chip", "SUPPLY_CHAIN": "box", "ENERGY": "atom",
               "REGULATION": "scale", "GEOPOLITICS": "globe", "EXPORT_CONTROL": "shield", "M&A": "coins",
               "CAPEX": "building", "R&D": "atom"}


def trends(reader: Reader) -> dict:
    info = collection(reader)
    sigs = [signal_card(s) for s in reader.signals(page=Page.of(1, 20))["data"]]
    dates = reader.label_event_dates()
    tiles = []
    for r in trend_rows(reader):
        series = weekly(dates.get(r["code"], []), info["as_of"])
        tiles.append({**r, "icon": THEME_ICONS.get(r["code"], "radar"), "series": series, "spark": spark(series),
                      "total": sum(series), "baseline_txt": fr_num(r["baseline"])})
    sectors = [x for x in tiles if x["kind"] == "sector"]
    topics = sorted([x for x in tiles if x["kind"] != "sector"], key=lambda x: (x["mood"] != "up", -x["current"]))
    return {"info": info, "sectors": sectors, "topics": topics, "signals": sigs,
            "weeks_needed": max(0, 8 - (info["history_weeks"] or 0))}


COUNTRIES = {
    "FR": "France", "DE": "Allemagne", "IT": "Italie", "ES": "Espagne", "GB": "Royaume-Uni", "US": "États-Unis",
    "CN": "Chine", "PL": "Pologne", "BE": "Belgique", "NL": "Pays-Bas", "SE": "Suède", "NO": "Norvège",
    "FI": "Finlande", "DK": "Danemark", "UA": "Ukraine", "RU": "Russie", "IN": "Inde", "JP": "Japon",
    "CA": "Canada", "TR": "Turquie", "LU": "Luxembourg", "EU": "Union européenne", "BR": "Brésil", "INT": "International",
}
TYPE_LABELS = {"company": "Entreprise", "organization": "Institution", "programme": "Programme",
               "technology": "Technologie", "country": "Pays", "region": "Région", "material": "Matériau"}
TYPE_ICONS = {"company": "building", "organization": "landmark", "programme": "orbit", "technology": "chip",
              "country": "globe", "region": "globe", "material": "box"}
WEEKS = 12


def country_name(code: str) -> str:
    return COUNTRIES.get((code or "").upper(), code or "")


def actor_name(e: dict) -> str:
    """Countries in French; other actors keep their usual name."""
    if e["type"] in ("country", "region") and e.get("country"):
        return country_name(e["country"])
    return e["name"]


def weekly(dates: list[str], as_of: str | None, weeks: int = WEEKS) -> list[int]:
    """Events per week, oldest first; the last bar is the 7 days before the last collection."""
    end = _dt(as_of)
    counts = [0] * weeks
    if end is None:
        return counts
    for d in dates:
        x = _dt(d)
        if x is None:
            continue
        age = (end - x).days
        if 0 <= age < weeks * 7:
            counts[weeks - 1 - age // 7] += 1
    return counts


def spark(series: list[int], w: int = 120, h: int = 32) -> dict:
    """SVG geometry for a small line chart (points + filled area)."""
    top = max(series) or 1
    step = w / max(1, len(series) - 1)
    pts = [(round(i * step, 1), round(h - 3 - (v / top) * (h - 6), 1)) for i, v in enumerate(series)]
    line = " ".join(f"{x},{y}" for x, y in pts)
    return {"w": w, "h": h, "line": line, "area": f"0,{h} {line} {w},{h}", "last": pts[-1] if pts else (0, h)}


def _ago(value: str | None, as_of: str | None) -> str:
    d, end = _dt(value), _dt(as_of)
    if d is None or end is None:
        return ""
    days = (end - d).days
    return "aujourd'hui" if days <= 0 else ("hier" if days == 1 else (f"il y a {days} jours" if days < 45 else f"le {fr_date(value)}"))


def actors(reader: Reader, *, q: str, type_: str, sort: str = "cited") -> dict:
    info = collection(reader)
    res = reader.entities(q=q or None, type=type_ or None, min_events=1, page=Page.of(1, 100))
    rows = res["data"]
    dates = reader.entity_event_dates([r["id"] for r in rows]) if rows else {}
    top = max((r["event_count"] for r in rows), default=1) or 1
    cards = []
    for r in rows:
        series = weekly(dates.get(r["id"], []), info["as_of"])
        cards.append({
            "id": r["id"], "name": actor_name(r), "type": r["type"], "type_label": TYPE_LABELS.get(r["type"], r["type"]),
            "icon": TYPE_ICONS.get(r["type"], "nodes"), "country": country_name(r["country"]) if r["type"] not in ("country", "region") else "",
            "sector": label(r["sector"]) if r["sector"] and r["sector"] != "MULTI" else "",
            "events": r["event_count"], "share": round(100 * r["event_count"] / top),
            "last_seen": _ago(r["last_seen"], info["as_of"]), "last_raw": r["last_seen"] or "",
            "week": series[-1], "spark": spark(series, 96, 26),
        })
    if sort == "recent":
        cards.sort(key=lambda c: c["last_raw"], reverse=True)
    elif sort == "name":
        cards.sort(key=lambda c: c["name"].lower())
    elif sort == "rising":
        cards.sort(key=lambda c: (-c["week"], -c["events"]))
    counts = {t: reader.entities(type=t, min_events=1, page=Page.of(1, 1))["total"] for t, _, _ in ENTITY_TYPES}
    return {"info": info, "cards": cards, "total": res["total"], "sort": sort,
            "types": [(t, lab, counts[t]) for t, lab, _ in ENTITY_TYPES], "all_count": sum(counts.values())}


def actor(reader: Reader, entity_id: str, page: int = 1) -> dict | None:
    e = reader.entity(entity_id)
    if e is None:
        return None
    info = collection(reader)
    series = weekly(reader.entity_event_dates([entity_id]).get(entity_id, []), info["as_of"])
    res = reader.events(entity=entity_id, sort="recent", page=Page.of(page, 15))
    related: dict[str, int] = {}
    for rel in e["relations"]:
        other = rel["target"] if rel["source"]["id"] == entity_id else rel["source"]
        if other["id"] and other["id"] != entity_id:
            related[other["id"]] = related.get(other["id"], 0) + (rel["weight"] or 1)
    rel_rows = []
    for rid, w in sorted(related.items(), key=lambda kv: -kv[1])[:10]:
        ent = reader.con.execute("SELECT entity_id, canonical_name, entity_type, country FROM entities WHERE entity_id = ?", (rid,)).fetchone()
        if ent:
            rel_rows.append({"id": rid, "weight": w, "name": actor_name({"type": ent["entity_type"], "country": ent["country"],
                                                                          "name": ent["canonical_name"]}),
                             "icon": TYPE_ICONS.get(ent["entity_type"], "nodes")})
    top = max(series) or 1
    bars = [{"value": v, "height": round(100 * v / top), "label": f"semaine -{WEEKS - 1 - i}" if i < WEEKS - 1 else "7 derniers jours"}
            for i, v in enumerate(series)]
    return {
        "info": info, "a": {
            "id": e["id"], "name": actor_name(e), "type_label": TYPE_LABELS.get(e["type"], e["type"]),
            "icon": TYPE_ICONS.get(e["type"], "nodes"),
            "country": country_name(e["country"]) if e["type"] not in ("country", "region") else "",
            "sector": label(e["sector"]) if e["sector"] and e["sector"] != "MULTI" else "",
            "aliases": [x for x in e["aliases"] if x.lower() != e["name"].lower()][:8],
            "events": e["event_count"], "articles": e["item_count"],
            "first_seen": fr_date(e["first_seen"]), "last_seen": _ago(e["last_seen"], info["as_of"]),
        },
        "bars": bars, "week": series[-1], "related": rel_rows,
        "signals": [signal_card(s) for s in reader.signals(subject=entity_id, page=Page.of(1, 5))["data"]],
        "events": [event_card(x) for x in res["data"]], "total": res["total"], "page": res["page"], "pages": res["pages"],
    }


# ---------------------------------------------------------------------- collection status page
RUN_STATUS = {"ok": ("Réussie", "up"), "partial": ("Partielle", "flat"), "failed": ("Échec", "down"), "running": ("En cours", "flat")}
SOURCE_STATUS = {"ok": ("À jour", "up"), "warning": ("Avertissement", "flat"), "error": ("En erreur", "down"),
                 "never_run": ("Jamais collectée", "flat")}
FAMILY_FR = {"INS": "Institution", "IND": "Industriel", "PRS": "Presse spécialisée", "AGG": "Agrégateur",
             "VID": "Vidéo officielle", "RES": "Recherche"}
STAGE_FR = {"collect": "Collecte", "normalize_clean": "Nettoyage", "enrich_classify": "Acteurs et thèmes",
            "deduplicate": "Dédoublonnage", "events": "Événements", "score": "Scores", "relations": "Relations",
            "signals": "Signaux", "store": "Enregistrement"}
LOG_FR = {
    "SOURCE_UNREACHABLE": "Source injoignable", "TRANSPORT_ERROR": "Erreur réseau", "PARSE_ERROR": "Flux illisible",
    "EMPTY_FEED": "Flux vide", "XML_MALFORMED": "Flux abîmé, en partie récupéré", "ENCODING_OVERRIDE": "Encodage corrigé",
    "ENTRY_INVALID": "Entrée invalide ignorée", "DATE_INVALID": "Date illisible", "DATE_FUTURE": "Date dans le futur",
    "DATE_MISSING": "Date absente", "DATE_IMPLAUSIBLE": "Date improbable", "TITLE_DERIVED": "Titre reconstitué",
    "REJECTED_EMPTY": "Article vide rejeté", "INSUFFICIENT_HISTORY": "Historique insuffisant pour les signaux",
    "ORPHAN_SOURCE": "Articles d'une source retirée du registre", "NO_SOURCE_SELECTED": "Aucune source active",
    "SOURCE_DISABLED": "Source désactivée", "UNKNOWN_SOURCE": "Source inconnue", "STAGE_FAILED": "Étape en échec",
}


def _human_error(text: str | None) -> tuple[str, str]:
    """"CODE: message" → ("Libellé en français", "message court")."""
    if not text:
        return "", ""
    code, sep, rest = text.partition(":")
    if sep and code.isupper() and " " not in code:
        return LOG_FR.get(code, code.replace("_", " ").capitalize()), rest.strip()[:140]
    return "", text[:140]


def _run_error(summary: str | None, reader: Reader) -> str:
    """"source-id: CODE; …" → "NASA news releases : source injoignable ; …"."""
    if not summary:
        return ""
    out = []
    for part in summary.split(";"):
        sid, _, code = part.strip().partition(": ")
        name = reader.sources[sid]["source_name"] if sid in reader.sources else sid
        out.append(f"{name} : {LOG_FR.get(code.strip(), code.strip()).lower()}" if code else part.strip())
    return " ; ".join(out[:6]) + (f" ; et {len(out) - 6} autre(s)" if len(out) > 6 else "")


def _duration(seconds) -> str:
    s = int(round(seconds or 0))
    return f"{s} s" if s < 60 else f"{s // 60} min {s % 60:02d} s"


def collecte(reader: Reader) -> dict:
    info = collection(reader)
    st = reader.status()
    run = st["last_run"]
    last = None
    stages = []
    logs = []
    if run:
        label_, mood = RUN_STATUS.get(run["status"], (run["status"], "flat"))
        last = {"status": label_, "mood": mood, "date": fr_date(run["finished_at"] or run["started_at"], with_time=True),
                "duration": _duration(run["total_seconds"]), "error": _run_error(run["error_summary"], reader)}
        top = max((s.get("seconds") or 0 for s in run["stages"]), default=0) or 1
        for s in run["stages"]:
            stages.append({"name": STAGE_FR.get(s["stage"], s["stage"]), "seconds": f"{s.get('seconds') or 0:.1f}".replace(".", ","),
                           "share": max(2, round(100 * (s.get("seconds") or 0) / top))})
        detail = reader.run(run["run_id"], None, Page.of(1, 100))
        for entry in detail["logs"]["data"] if detail else []:
            if entry["level"] in ("error", "warning"):
                logs.append({"level": entry["level"], "what": LOG_FR.get(entry["code"], entry["code"]),
                             "source": reader.sources[entry["source_id"]]["source_name"] if entry["source_id"] in reader.sources else (entry["source_id"] or ""),
                             "message": (entry["message"] or "").splitlines()[0][:220]})
    sources = []
    for s in reader.source_list()["data"]:
        if not s["enabled"]:
            status, mood = "Désactivée", "off"
        else:
            status, mood = SOURCE_STATUS.get(s["status"], (s["status"], "flat"))
        sources.append({"name": s["name"], "family": FAMILY_FR.get(s["family"], s["family"]), "status": status, "mood": mood,
                        "new": s["new_items_last_run"] or 0, "fetched": s["items_last_run"] or 0, "stored": s["items_stored"],
                        "success": _ago(s["last_success_at"], info["as_of"]) if s["last_success_at"] else "jamais",
                        "error": _human_error(s["last_error"]) if s["status"] in ("error", "warning") and s["enabled"] else ("", ""),
                        "enabled": s["enabled"]})
    order = {"down": 0, "flat": 1, "up": 2, "off": 3}
    sources.sort(key=lambda s: (order.get(s["mood"], 1), s["name"].lower()))
    runs = []
    for r in reader.runs(Page.of(1, 10))["data"]:
        label_, mood = RUN_STATUS.get(r["status"], (r["status"], "flat"))
        m = r["metrics"] or {}
        runs.append({"date": fr_date(r["started_at"], with_time=True), "status": label_, "mood": mood,
                     "duration": _duration(r["total_seconds"]), "new": m.get("new_raw", "–"), "events": m.get("events", "–"),
                     "sources": f"{(m.get('sources_ok', 0) + m.get('sources_warning', 0))}/{m.get('sources_selected', 0)}" if m else "–"})
    briefs = 0
    if reader._has_briefs:
        briefs = reader.con.execute("SELECT COUNT(DISTINCT b.event_id) FROM event_briefs b JOIN events e ON e.event_id = b.event_id").fetchone()[0]
    return {"info": info, "last": last, "stages": stages, "logs": logs, "sources": sources, "runs": runs,
            "briefs": briefs, "briefs_share": round(100 * briefs / info["events"]) if info["events"] else 0,
            "sources_bad": sum(1 for s in sources if s["mood"] == "down")}
