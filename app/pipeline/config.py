"""Load the pipeline configuration and compute the versions recorded on every row."""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from app.config import BASE_DIR
from app.config import LIVE_DB as _LIVE_DB
from app.pipeline import PROCESSING_VERSION

CONFIG_DIR = BASE_DIR / "config" / "pipeline"
FIXTURES_DIR = BASE_DIR / "data" / "fixtures"
DEFAULT_DB = BASE_DIR / "data" / "pipeline" / "sector_intel.sqlite"   # fixtures (fictitious data)
LIVE_DB = _LIVE_DB                                                       # live collection (real data), see SECTOR_INTEL_DATA_DIR


def default_db(mode: str) -> Path:
    """Fixtures and live data never share a database: mixing them would put fictitious
    events next to real ones, and fixture source ids are unknown to the live registry."""
    return LIVE_DB if mode == "live" else DEFAULT_DB

RULE_FILES = ("entities.toml", "taxonomy.toml", "scoring.toml")
FAMILIES = ("INS", "IND", "PRS", "AGG", "VID", "RES")
SOURCE_TYPES = ("institutional", "industrial", "press", "aggregator", "video", "research")
CONNECTORS = ("rss", "google_news", "gdelt", "youtube", "arxiv", "hal")


class ConfigError(ValueError):
    """A configuration file is invalid. Raised before any collection starts."""


@dataclass(frozen=True)
class SourceConfig:
    id: str
    name: str
    connector: str
    source_type: str
    family: str
    tier: int
    domain: str = ""
    country: str = ""
    institution: str = ""
    publisher_entity: str = ""
    language: str = ""
    url: str = ""
    fixture: tuple[str, ...] = ()
    enabled: bool = True
    baseline_credibility: float | None = None
    min_interval_s: float = 1.0
    timeout_s: float = 15.0
    max_items: int = 0                  # 0 = keep every entry; else keep the N most recent per run


@dataclass
class PipelineConfig:
    sources: list[SourceConfig]
    entities: dict
    taxonomy: dict
    scoring: dict
    sources_file: str
    rules_version: str
    model_version: str = "tfidf-1;lang-stopwords-1;embedder:none"
    processing_version: str = PROCESSING_VERSION
    extra: dict = field(default_factory=dict)

    def source(self, source_id: str) -> SourceConfig | None:
        return next((s for s in self.sources if s.id == source_id), None)

    def baseline(self, src: SourceConfig) -> float:
        if src.baseline_credibility is not None:
            return float(src.baseline_credibility)
        return float(self.scoring["credibility"]["tier_baseline"][str(src.tier)])


def _load(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"Missing configuration file: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path.name}: {exc}") from exc


def _source(raw: dict, file: str) -> SourceConfig:
    missing = [k for k in ("id", "name", "connector", "source_type", "family", "tier") if k not in raw]
    if missing:
        raise ConfigError(f"{file}: source {raw.get('id', '?')} lacks {', '.join(missing)}")
    if raw["connector"] not in CONNECTORS:
        raise ConfigError(f"{file}: source {raw['id']} has unknown connector {raw['connector']!r}")
    if raw["family"] not in FAMILIES:
        raise ConfigError(f"{file}: source {raw['id']} has unknown family {raw['family']!r}")
    if raw["source_type"] not in SOURCE_TYPES:
        raise ConfigError(f"{file}: source {raw['id']} has unknown source_type {raw['source_type']!r}")
    if int(raw["tier"]) not in (1, 2, 3, 4):
        raise ConfigError(f"{file}: source {raw['id']} tier must be 1-4")
    fixture = raw.get("fixture", ())
    if isinstance(fixture, str):
        fixture = (fixture,)
    return SourceConfig(
        id=raw["id"], name=raw["name"], connector=raw["connector"], source_type=raw["source_type"],
        family=raw["family"], tier=int(raw["tier"]), domain=raw.get("domain", ""),
        country=raw.get("country", ""), institution=raw.get("institution", ""),
        publisher_entity=raw.get("publisher_entity", ""), language=raw.get("language", ""),
        url=raw.get("url", ""), fixture=tuple(fixture), enabled=bool(raw.get("enabled", True)),
        baseline_credibility=raw.get("baseline_credibility"),
        min_interval_s=float(raw.get("min_interval_s", 1.0)), timeout_s=float(raw.get("timeout_s", 15.0)),
        max_items=int(raw.get("max_items", 0)),
    )


def rules_hash(config_dir: Path = CONFIG_DIR) -> str:
    """Hash of the rule files: any change to a threshold or an alias changes it."""
    digest = hashlib.sha256()
    for name in RULE_FILES:
        digest.update(name.encode())
        digest.update((config_dir / name).read_bytes())
    return digest.hexdigest()[:12]


def load_config(sources_file: str = "sources.toml", config_dir: Path = CONFIG_DIR) -> PipelineConfig:
    raw_sources = _load(config_dir / sources_file)
    sources = [_source(s, sources_file) for s in raw_sources.get("source", [])]
    ids = [s.id for s in sources]
    if len(ids) != len(set(ids)):
        raise ConfigError(f"{sources_file}: duplicate source ids")
    entities = _load(config_dir / "entities.toml")
    taxonomy = _load(config_dir / "taxonomy.toml")
    scoring = _load(config_dir / "scoring.toml")
    known = {e["id"] for e in entities.get("entity", [])}
    for s in sources:
        if s.publisher_entity and s.publisher_entity not in known:
            raise ConfigError(f"{sources_file}: {s.id}.publisher_entity {s.publisher_entity!r} is not in entities.toml")
    return PipelineConfig(
        sources=sources, entities=entities, taxonomy=taxonomy, scoring=scoring,
        sources_file=sources_file, rules_version=rules_hash(config_dir),
    )
