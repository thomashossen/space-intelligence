"""ENRICH: entity extraction against the controlled nomenclature (entities.toml).

Deterministic and explainable: each mention records the alias that matched, its
position and the method (``alias`` or ``publisher``). "Airbus", "Airbus SE" and
"AIRBUS" resolve to the same entity id because they are aliases of one block.
No statistical NER: an unknown company is not invented, it is simply not
extracted (see limits in the README).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TYPE_FIELDS = {
    "company": "companies",
    "organization": "organizations",
    "technology": "technologies",
    "material": "technologies",
    "programme": "programmes",
}


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    type: str
    aliases: tuple[str, ...] = ()
    country: str = ""
    sector: str = ""
    parent: str = ""


@dataclass
class Mention:
    entity_id: str
    entity_type: str
    surface: str
    start: int
    end: int
    method: str = "alias"

    def as_dict(self) -> dict:
        return {"entity_id": self.entity_id, "type": self.entity_type, "surface": self.surface,
                "start": self.start, "end": self.end, "method": self.method}


def _case_sensitive(alias: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", alias)
    return bool(letters) and letters.isupper() and len(letters) <= 5


@dataclass
class Gazetteer:
    entities: dict[str, Entity]
    version: str = ""
    _ci: re.Pattern | None = field(default=None, repr=False)
    _cs: re.Pattern | None = field(default=None, repr=False)
    _lookup_ci: dict[str, str] = field(default_factory=dict, repr=False)
    _lookup_cs: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_config(cls, cfg: dict) -> "Gazetteer":
        ents = {}
        for raw in cfg.get("entity", []):
            ents[raw["id"]] = Entity(
                id=raw["id"], name=raw["name"], type=raw["type"], aliases=tuple(raw.get("aliases", [])),
                country=raw.get("country", ""), sector=raw.get("sector", ""), parent=raw.get("parent", ""),
            )
        gaz = cls(ents, version=str(cfg.get("version", "")))
        gaz._compile()
        return gaz

    def _compile(self) -> None:
        ci, cs = {}, {}
        for e in self.entities.values():
            for alias in (e.name, *e.aliases):
                alias = alias.strip()
                if not alias:
                    continue
                if _case_sensitive(alias):
                    cs.setdefault(alias, e.id)
                else:
                    ci.setdefault(alias.lower(), e.id)
        self._lookup_ci, self._lookup_cs = ci, cs

        def pattern(aliases, flags):
            alts = sorted(aliases, key=len, reverse=True)
            body = "|".join(re.escape(a) for a in alts)
            return re.compile(rf"(?<![\w])(?:{body})(?![\w])", flags)

        self._ci = pattern(ci.keys(), re.IGNORECASE)
        self._cs = pattern(cs.keys(), 0)

    def resolve(self, name: str) -> Entity | None:
        """Normalise a surface form to its entity (Airbus SE → airbus)."""
        key = name.strip()
        eid = self._lookup_cs.get(key) or self._lookup_ci.get(key.lower())
        return self.entities.get(eid) if eid else None

    def find(self, text: str) -> list[Mention]:
        spans = []
        for m in self._ci.finditer(text):
            spans.append((m.start(), m.end(), self._lookup_ci[m.group(0).lower()], m.group(0)))
        for m in self._cs.finditer(text):
            spans.append((m.start(), m.end(), self._lookup_cs[m.group(0)], m.group(0)))
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        kept, end = [], -1
        for start, stop, eid, surface in spans:
            if start < end:
                continue                      # overlaps a longer, earlier match
            kept.append(Mention(eid, self.entities[eid].type, surface, start, stop))
            end = stop
        return kept


def extract(item, gaz: Gazetteer, publisher_entity: str = "") -> None:
    """Fill the entity fields of an Item in place."""
    text = f"{item.title}\n{item.summary}\n{item.cleaned_content}"
    mentions = gaz.find(text)
    if publisher_entity and publisher_entity in gaz.entities:
        e = gaz.entities[publisher_entity]
        mentions.append(Mention(e.id, e.type, e.name, -1, -1, "publisher"))
    fields: dict[str, list[str]] = {"companies": [], "organizations": [], "technologies": [], "programmes": []}
    countries: list[str] = []
    for m in mentions:
        ent = gaz.entities[m.entity_id]
        target = TYPE_FIELDS.get(ent.type)
        if target and ent.id not in fields[target]:
            fields[target].append(ent.id)
        if ent.type in ("country", "region") and ent.country and ent.country not in countries:
            countries.append(ent.country)
        if ent.type == "organization" and ent.country and ent.country != "INT" and ent.country not in countries:
            countries.append(ent.country)
    item.companies = fields["companies"]
    item.organizations = fields["organizations"]
    item.technologies = fields["technologies"]
    item.programmes = fields["programmes"]
    item.countries = countries
    # One record per entity with its first surface form and number of mentions.
    agg: dict[str, dict] = {}
    for m in mentions:
        rec = agg.setdefault(m.entity_id, {"entity_id": m.entity_id, "type": m.entity_type, "surface": m.surface,
                                           "count": 0, "method": m.method, "first_offset": m.start})
        rec["count"] += 1
    item.mentions = list(agg.values())
