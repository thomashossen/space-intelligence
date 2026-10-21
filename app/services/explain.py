"""Plain-French explanation of an event's scores, for readers who are not analysts.

The pipeline stores every score with its components (value, weight, contribution and a
technical note). This module turns each component into one sentence, by reading the
values themselves, never by inventing anything: what was measured, what was found, and
how many points it brought.
"""

from __future__ import annotations

import re

FAMILY_TIER = {1: "institution ou source primaire", 2: "presse spécialisée ou recherche", 3: "agrégateur",
               4: "source peu fiable"}


def _fmt(x: float) -> str:
    x = round(float(x), 1)
    return (str(int(x)) if x == int(x) else f"{x:.1f}").replace(".", ",")


def _pts(x: float, signed: bool = True) -> str:
    s = _fmt(abs(x))
    if round(abs(x), 1) == 0:
        return "0 pt"
    if not signed:
        return f"{s} pt{'s' if abs(x) >= 2 else ''}"
    return f"{'−' if x < 0 else '+'}{s} pt{'s' if abs(x) >= 2 else ''}"


def _level(v: int) -> tuple[str, str]:
    if v >= 70:
        return "élevé", "up"
    if v >= 40:
        return "moyen", "flat"
    return "faible", "down"


def _int(pattern: str, text: str, default: int | None = None) -> int | None:
    m = re.search(pattern, text or "")
    return int(float(m.group(1))) if m else default


class Explainer:
    def __init__(self, names: dict[str, str], labels: dict[str, str], countries: dict[str, str], event_types: dict[str, str]):
        self.names, self.labels, self.countries, self.event_types = names, labels, countries, event_types

    # ------------------------------------------------------------------ helpers
    def _entity(self, ident: str) -> str:
        ident = ident.strip()
        if ident.startswith(("country-", "region-")):
            code = ident.split("-", 1)[1].upper()
            return self.countries.get(code, self.names.get(ident, code))
        return self.names.get(ident, ident)

    def _label(self, code: str) -> str:
        return self.labels.get(code, code.replace("_", " ").capitalize())

    def _triggers(self, note: str) -> str:
        """'GEOPOLITICS +45; country-cn +15; 4 countries; event type launch +20' → French list."""
        if not note or note.startswith("no rule"):
            return ""
        out = []
        for part in note.split(";"):
            part = part.strip()
            m = re.match(r"event type (\w+) \+(\d+)", part)
            if m:
                out.append(f"type d'événement « {self.event_types.get(m.group(1), m.group(1))} » (+{m.group(2)})")
                continue
            m = re.match(r"(\d+) (countries|companies|material|technology|programmes?)\b", part)
            if m:
                n = int(m.group(1))
                word = {"countries": ("pays", "pays"), "companies": ("entreprise", "entreprises"),
                        "material": ("matériau critique", "matériaux critiques"),
                        "technology": ("technologie", "technologies"), "programme": ("programme", "programmes"),
                        "programmes": ("programme", "programmes")}[m.group(2)]
                out.append(f"{n} {word[0] if n == 1 else word[1]}")
                continue
            m = re.match(r"([\w&\-]+) \+(\d+)", part)
            if m:
                code = m.group(1)
                name = self._label(code) if code.upper() == code else self._entity(code)
                out.append(f"{name} (+{m.group(2)})")
                continue
            out.append(part)
        return ", ".join(out)

    # ------------------------------------------------------------------ importance
    def _importance(self, c: dict, ev: dict) -> str:
        k, note, v = c["key"], c.get("note", ""), c.get("value", 0)
        if k == "reliability":
            return f"La source la plus fiable de l'événement est notée {_fmt(v)} sur 100."
        if k == "origins":
            n = _int(r"(\d+) origin", note, ev["proof"]["orig"])
            return (f"{n} origine{'s' if n > 1 else ''} indépendante{'s' if n > 1 else ''} : plus un fait est rapporté "
                    "par des sources différentes, plus il compte (20 points de base, 20 de plus par origine).")
        if k == "relevance":
            themes = [self._label(x) for x in ev["sectors"] + ev["topics"]][:5]
            return ("Thèmes du secteur détectés : " + ", ".join(themes) + ".") if themes else "Peu de thèmes du secteur détectés."
        if k == "recency":
            if "estimated" in note or "unknown" in note:
                return "Date de publication inconnue : valeur neutre de 50."
            h = _int(r"first seen (\d+) h", note)
            if h is None:
                return "Fraîcheur de l'information."
            age = f"{h} heure{'s' if h > 1 else ''}" if h < 48 else f"{h // 24} jours"
            return f"Repéré {age} avant la collecte. La fraîcheur est divisée par deux chaque jour."
        if k == "impact":
            return f"Portée stratégique de l'événement ({_fmt(v)} sur 100), détaillée plus bas."
        return note

    # ------------------------------------------------------------------ credibility
    def _credibility(self, c: dict) -> str:
        k, note, v = c["key"], c.get("note", ""), c.get("value", 0)
        if k == "source_reliability":
            m = re.search(r"\(tier (\d), (.+)\)", note)
            if m:
                tier = int(m.group(1))
                return f"Meilleure source : {m.group(2)}, {FAMILY_TIER.get(tier, 'source')} (niveau {tier} sur 4)."
            return "Fiabilité habituelle de la meilleure source."
        if k == "primary_source_bonus":
            return ("L'acteur concerné publie lui-même l'information (source primaire)." if v > 0
                    else "Aucune source primaire : l'acteur concerné ne s'est pas exprimé lui-même.")
        if k == "corroboration_bonus":
            n = _int(r"(\d+) independent", note, 1)
            f = _int(r"(\d+) famil", note, 1)
            if n <= 1:
                return "Une seule origine : l'information n'est pas encore recoupée."
            return f"Recoupé par {n} origines indépendantes, issues de {f} type{'s' if f > 1 else ''} de sources différents."
        if k == "temporal_consistency":
            p = _int(r"(\d+) %", note, 0)
            return f"{p} % des sources ont une date de publication fiable."
        if k == "contradiction_penalty":
            return ("Aucune contradiction entre les sources." if v == 0 or "none" in note
                    else "Les sources donnent des chiffres différents : l'information est contestée.")
        return note

    # ------------------------------------------------------------------ novelty
    def _novelty(self, c: dict) -> str:
        k, note = c["key"], c.get("note", "")
        if "insufficient history" in note:
            return "Non évalué : pas encore assez d'historique."
        if k == "new_topic":
            if note.startswith("rare before:"):
                return "Thème rare jusqu'ici : " + ", ".join(self._label(x.strip()) for x in note.split(":", 1)[1].split(",")) + "."
            return "Thèmes déjà fréquents dans l'actualité récente."
        if k == "new_actor":
            if ":" in note and not note.startswith("no "):
                return "Première apparition récente de : " + ", ".join(self._entity(x) for x in note.split(":", 1)[1].split(",")) + "."
            return "Aucun nouvel acteur."
        if k == "new_technology":
            if ":" in note and not note.startswith("no "):
                return "Première mention de : " + ", ".join(self._entity(x) for x in note.split(":", 1)[1].split(",")) + "."
            return "Aucune nouvelle technologie."
        if k == "new_relation":
            if ":" in note and not note.startswith("no "):
                pairs = [" et ".join(self._entity(y) for y in x.split("×")) for x in note.split(":", 1)[1].split(";") if x.strip()]
                return "Première association observée entre " + " ; ".join(pairs[:3]) + "."
            return "Aucune association nouvelle entre acteurs."
        if k == "frequency_change":
            m = re.search(r"z-score ([\d.]+) \((.+)\)", note)
            if m:
                return (f"Le thème « {self._label(m.group(2))} » est nettement plus actif que d'habitude "
                        f"(écart de {_fmt(float(m.group(1)))} fois sa variation normale).")
            return "Aucun thème plus actif que d'habitude."
        if k == "dissimilarity":
            m = re.search(r"1 − ([\d.]+) \(closest of (\d+)", note)
            if m:
                return (f"Comparé aux {m.group(2)} événements des 90 derniers jours, le plus proche lui ressemble "
                        f"à {round(float(m.group(1)) * 100)} %.")
            return "Aucun événement comparable dans les 90 derniers jours."
        return note

    # ------------------------------------------------------------------ public
    def sections(self, ev: dict) -> list[dict]:
        expl = ev.get("explanation") or {}
        s = ev["scores"]
        out = []

        imp = expl.get("importance", {})
        out.append(self._section(
            "Importance", s["importance"],
            "Combien cet événement compte pour le secteur. C'est une moyenne pondérée de cinq critères.",
            [{"label": {"reliability": "Fiabilité des sources", "origins": "Nombre d'origines", "relevance": "Pertinence pour le secteur",
                        "recency": "Fraîcheur", "impact": "Impact potentiel"}.get(c["key"], c.get("label", c["key"])),
              "text": self._importance(c, ev), "points": _pts(c.get("contribution", 0)),
              "detail": f"{_fmt(c.get('value', 0))}/100 × poids {round(c.get('weight', 0) * 100)} %"}
             for c in imp.get("components", [])],
            "À partir de 65, l'événement est considéré comme prioritaire." if imp.get("priority") is not None else ""))
        if imp.get("priority"):
            out[-1].update(level="prioritaire", mood="up")

        cred = expl.get("credibility", {})
        out.append(self._section(
            "Crédibilité", s["credibility"],
            "La confiance qu'on peut accorder à l'information : qui la rapporte et si elle est recoupée.",
            [{"label": {"source_reliability": "Fiabilité de la source", "primary_source_bonus": "Source primaire",
                        "corroboration_bonus": "Recoupement", "temporal_consistency": "Dates cohérentes",
                        "contradiction_penalty": "Contradictions"}.get(c["key"], c["key"]),
              "text": self._credibility(c), "points": _pts(-abs(c.get("value", 0)) if c["key"] == "contradiction_penalty" else c.get("value", 0)),
              "detail": ""}
             for c in cred.get("components", [])],
            "Le niveau d'une source compte, mais n'est jamais un verdict à lui seul."))

        nov = expl.get("novelty", {})
        out.append(self._section(
            "Nouveauté", s["novelty"],
            "Ce que l'événement apporte de neuf par rapport aux semaines précédentes. Les reprises d'un même article n'ajoutent rien.",
            [{"label": {"new_topic": "Thème nouveau", "new_actor": "Acteur nouveau", "new_technology": "Technologie nouvelle",
                        "new_relation": "Relation nouvelle", "frequency_change": "Accélération d'un thème",
                        "dissimilarity": "Différence avec les événements passés"}.get(c["key"], c["key"]),
              "text": self._novelty(c), "points": _pts(c.get("value", 0)), "detail": ""}
             for c in nov.get("components", [])], ""))

        strat = expl.get("strategic", {})
        dims = {"geopolitical": ("Géopolitique", "relations entre États, alliances, rapports de force"),
                "industrial": ("Industrie", "commandes, capacités de production, position des industriels"),
                "technological": ("Technologie", "technologies critiques ou émergentes"),
                "supply_chain": ("Approvisionnement", "matières, composants et fournisseurs critiques"),
                "sovereignty": ("Souveraineté", "autonomie stratégique européenne ou nationale")}
        rows = []
        for c in strat.get("components", []):
            name, about = dims.get(c["key"], (c["key"], ""))
            trig = self._triggers(c.get("note", ""))
            rows.append({"label": name, "text": (f"{about[:1].upper() + about[1:]}. " if about else "")
                         + (f"Déclencheurs : {trig}." if trig else "Aucun déclencheur."),
                         "points": _pts(c.get("contribution", 0)),
                         "detail": f"{_fmt(c.get('value', 0))}/100 × poids {round(c.get('weight', 0) * 100)} %"})
        out.append(self._section(
            "Portée stratégique", s["strategic"],
            "Pourquoi l'événement peut peser au-delà de l'actualité du jour, selon cinq dimensions.", rows,
            "Les déclencheurs sont des règles fixes (thèmes, acteurs, pays détectés), sans interprétation."))
        return out

    @staticmethod
    def _section(name: str, value: int, what: str, rows: list[dict], foot: str) -> dict:
        level, mood = _level(value or 0)
        return {"name": name, "value": value or 0, "level": level, "mood": mood, "what": what, "rows": rows, "foot": foot}
