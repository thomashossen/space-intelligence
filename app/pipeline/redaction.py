"""REDACTION: French briefs for the most important events.

For each selected event, the writer receives the text of its independent origins
(title, feed summary and, when allowed, the article page itself) and returns a
structured French brief: title, summary, key points, stakes, context, what to
watch. Briefs are stored in ``event_briefs`` and never overwrite pipeline data.

Providers: ``local`` (no language model: sentences extracted from the articles,
translated offline into French, see translate.py), ``groq`` and ``mistral`` (free tiers, OpenAI-compatible APIs, key in
GROQ_API_KEY / MISTRAL_API_KEY), ``openai`` (any other OpenAI-compatible endpoint,
set ``base_url`` and ``api_key_env``), ``anthropic`` (Claude API, key in
ANTHROPIC_API_KEY) or ``ollama`` (local model). ``none`` disables the stage. The cache key is the event id
plus its set of origins, the model and the prompt version: a brief is written
again only when a new origin joins the event or the prompt changes.

Guard rails: the prompt forbids facts absent from the sources in the title,
summary and key points; the output is validated against a strict schema; a
brief is always displayed with its sources and labelled as generated.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.pipeline.config import CONFIG_DIR
from app.pipeline.connectors.base import USER_AGENT
from app.pipeline.text import clean_text

UTC = timezone.utc
PROMPT_VERSION = "fr-brief-1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_briefs (
  brief_key TEXT PRIMARY KEY, event_id TEXT, created_at TEXT, provider TEXT, model TEXT,
  prompt_version TEXT, origins INTEGER, article_pages INTEGER, brief TEXT
);
CREATE INDEX IF NOT EXISTS ix_briefs_event ON event_briefs(event_id, created_at);
"""

SYSTEM = """Tu es analyste dans une cellule de veille sur le spatial et la défense. Tes lecteurs sont francophones : \
étudiants, professionnels et curieux. Tu rédiges une fiche claire à partir des extraits de sources fournis.

Règles :
- Le titre, le résumé et les points clés reposent UNIQUEMENT sur les sources fournies. N'ajoute aucun chiffre, date, \
nom ou montant qui n'y figure pas.
- Les enjeux et le contexte peuvent s'appuyer sur des connaissances générales stables (rôle d'un acteur, ce qu'est \
un programme, pourquoi une technologie compte), sans affirmer de faits récents absents des sources.
- Si les sources se contredisent, dis-le explicitement. Si elles ne donnent qu'un titre, dis-le et reste bref.
- Français courant, phrases courtes, ton neutre, sans superlatifs ni formules promotionnelles. Explique les sigles \
la première fois (par exemple « DGA, la Direction générale de l'armement »).

Réponds UNIQUEMENT avec un objet JSON, sans texte autour, avec ces clés :
{"titre": "titre informatif en français, 110 caractères maximum",
 "resume": "3 à 5 phrases : qui, quoi, quand, combien, selon les sources",
 "points_cles": ["2 à 4 faits précis tirés des sources"],
 "enjeux": ["2 ou 3 éléments, chacun sous la forme « Libellé : explication en une ou deux phrases »"],
 "contexte": "2 à 4 phrases pour situer l'événement",
 "a_surveiller": ["1 à 3 prochaines étapes ou questions ouvertes"],
 "sources_limitees": true ou false}"""


@dataclass
class RedactionConfig:
    provider: str = "none"
    model: str = ""
    max_events_per_run: int = 30
    min_importance: int = 0
    fetch_articles: bool = True
    max_chars_per_source: int = 2500
    timeout_s: float = 90.0
    ollama_url: str = "http://localhost:11434"
    base_url: str = ""          # openai-compatible providers; presets fill it
    api_key_env: str = ""
    min_interval_s: float = -1  # pause between two calls; -1 = the preset's value
    json_mode: bool = True

    @classmethod
    def load(cls, path: Path = CONFIG_DIR / "redaction.toml") -> "RedactionConfig":
        raw = {}
        if path.exists():
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
        cfg = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        cfg.provider = os.getenv("SECTOR_INTEL_REDACTION_PROVIDER", cfg.provider)
        cfg.model = os.getenv("SECTOR_INTEL_REDACTION_MODEL", cfg.model)
        return cfg


class WriterError(RuntimeError):
    pass


# ---------------------------------------------------------------------- providers
class Writer:
    name = "base"

    def __init__(self, cfg: RedactionConfig):
        self.cfg = cfg
        self.model = cfg.model

    def complete(self, system: str, user: str) -> str:  # pragma: no cover - network
        raise NotImplementedError


class AnthropicWriter(Writer):  # pragma: no cover - network
    name = "anthropic"

    def __init__(self, cfg: RedactionConfig):
        super().__init__(cfg)
        import httpx  # noqa: PLC0415

        self.key = os.getenv("ANTHROPIC_API_KEY", "")
        if not self.key:
            raise WriterError("ANTHROPIC_API_KEY is not set")
        self.model = cfg.model or "claude-haiku-5-5"
        self.client = httpx.Client(timeout=cfg.timeout_s)

    def complete(self, system: str, user: str) -> str:
        res = self.client.post("https://api.anthropic.com/v1/messages", headers={
            "x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": self.model, "max_tokens": 2000, "system": system,
                  "messages": [{"role": "user", "content": user}]})
        if res.status_code != 200:
            raise WriterError(f"Claude API HTTP {res.status_code}: {res.text[:200]}")
        return "".join(b.get("text", "") for b in res.json().get("content", []) if b.get("type") == "text")


class OllamaWriter(Writer):  # pragma: no cover - network
    name = "ollama"

    def __init__(self, cfg: RedactionConfig):
        super().__init__(cfg)
        import httpx  # noqa: PLC0415

        self.model = cfg.model or "mistral-small3.2"
        self.client = httpx.Client(timeout=cfg.timeout_s)

    def complete(self, system: str, user: str) -> str:
        try:
            res = self.client.post(f"{self.cfg.ollama_url}/api/chat", json={
                "model": self.model, "stream": False, "format": "json", "options": {"temperature": 0.2},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        except Exception as exc:  # noqa: BLE001
            raise WriterError(f"Ollama unreachable at {self.cfg.ollama_url} ({exc.__class__.__name__}). Is `ollama serve` running?") from exc
        if res.status_code != 200:
            raise WriterError(f"Ollama HTTP {res.status_code}: {res.text[:200]}")
        return res.json().get("message", {}).get("content", "")


# Free tiers of OpenAI-compatible APIs. Limits change: check each console.
PRESETS = {
    # ~30 requests/min, ~1,000/day on the free plan (per model); very fast.
    "groq": {"base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY",
             "model": "llama-3.3-70b-versatile", "min_interval_s": 2.5},
    # "Experiment" plan: free, phone verification, about 2 requests/min; strong French.
    "mistral": {"base_url": "https://api.mistral.ai/v1", "api_key_env": "MISTRAL_API_KEY",
                "model": "mistral-small-latest", "min_interval_s": 31.0},
    "openai": {"base_url": "", "api_key_env": "OPENAI_API_KEY", "model": "", "min_interval_s": 1.0},
}


class OpenAICompatWriter(Writer):
    """Chat Completions API (Groq, Mistral, OpenRouter, any compatible endpoint).
    Paces calls, honours Retry-After on HTTP 429, falls back from JSON mode if refused."""

    def __init__(self, cfg: RedactionConfig, client=None):
        super().__init__(cfg)
        preset = PRESETS.get(cfg.provider, PRESETS["openai"])
        self.name = cfg.provider
        self.base_url = (cfg.base_url or preset["base_url"]).rstrip("/")
        self.model = cfg.model or preset["model"]
        self.interval = cfg.min_interval_s if cfg.min_interval_s >= 0 else preset["min_interval_s"]
        env = cfg.api_key_env or preset["api_key_env"]
        self.key = os.getenv(env, "")
        if not self.base_url or not self.model:
            raise WriterError("set base_url and model in config/pipeline/redaction.toml")
        if not self.key:
            raise WriterError(f"{env} is not set (export {env}=…)")
        if client is None:
            import httpx  # noqa: PLC0415

            client = httpx.Client(timeout=cfg.timeout_s)
        self.client = client
        self.json_mode = cfg.json_mode
        self._last = 0.0
        self.sleep = time.sleep

    def _post(self, body: dict):
        wait = self.interval - (time.monotonic() - self._last)
        if wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()
        return self.client.post(f"{self.base_url}/chat/completions", json=body,
                                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})

    def complete(self, system: str, user: str) -> str:
        body = {"model": self.model, "temperature": 0.2, "max_tokens": 1500,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        for attempt in range(4):
            if self.json_mode:
                body["response_format"] = {"type": "json_object"}
            else:
                body.pop("response_format", None)
            try:
                res = self._post(body)
            except Exception as exc:  # noqa: BLE001
                raise WriterError(f"{self.name} unreachable ({exc.__class__.__name__})") from exc
            if res.status_code == 429 and attempt < 3:
                ra = res.headers.get("retry-after", "")
                self.sleep(min(float(ra), 90.0) if ra.replace(".", "", 1).isdigit() else 20.0 * (attempt + 1))
                continue
            if res.status_code == 400 and self.json_mode and "response_format" in res.text:
                self.json_mode = False          # this model refuses JSON mode: ask in plain text
                continue
            if res.status_code != 200:
                hint = " (model unknown? run: python -m app.pipeline redact --list-models)" if res.status_code == 404 else ""
                raise WriterError(f"{self.name} HTTP {res.status_code}: {res.text[:160]}{hint}")
            choices = res.json().get("choices") or [{}]
            return (choices[0].get("message") or {}).get("content") or ""
        raise WriterError(f"{self.name}: rate limited (HTTP 429) after 3 retries; the daily quota may be reached")

    def models(self) -> list[str]:
        res = self.client.get(f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.key}"})
        if res.status_code != 200:
            raise WriterError(f"{self.name} HTTP {res.status_code}: {res.text[:160]}")
        return sorted(m.get("id", "") for m in res.json().get("data", []))


def make_writer(cfg: RedactionConfig) -> Writer | None:
    if cfg.provider == "local":
        from app.pipeline.translate import LocalWriter, TranslatorMissing  # noqa: PLC0415

        try:
            return LocalWriter()
        except TranslatorMissing as exc:
            raise WriterError(str(exc)) from exc
    if cfg.provider in PRESETS:
        return OpenAICompatWriter(cfg)
    if cfg.provider == "anthropic":
        return AnthropicWriter(cfg)
    if cfg.provider == "ollama":
        return OllamaWriter(cfg)
    if cfg.provider in ("", "none"):
        return None
    raise WriterError(f"unknown provider {cfg.provider!r} (none | local | groq | mistral | openai | anthropic | ollama)")


# ---------------------------------------------------------------------- article pages
_BLOCK = re.compile(r"<(article|main)\b[^>]*>(.*?)</\1>", re.S | re.I)


def article_text(html: str, limit: int) -> str:
    """Main text of an article page: <article> or <main> when present, paragraph-like lines only."""
    m = _BLOCK.search(html)
    text, _ = clean_text(m.group(2) if m else html)
    paras = [p for p in text.split("\n\n") if len(p) >= 60]
    return "\n".join(paras)[:limit]


class PageFetcher:  # pragma: no cover - network
    def __init__(self, timeout: float = 12.0):
        import httpx  # noqa: PLC0415

        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=timeout)
        self._last: dict[str, float] = {}

    def __call__(self, url: str, limit: int) -> str:
        host = re.sub(r"^https?://([^/]+).*", r"\1", url)
        if "news.google.com" in host or not url.startswith("http"):
            return ""                       # redirect pages need JavaScript; never decoded
        wait = 2.0 - (time.monotonic() - self._last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()
        try:
            res = self.client.get(url)
        except Exception:  # noqa: BLE001
            return ""
        if res.status_code != 200 or "html" not in res.headers.get("content-type", ""):
            return ""
        return article_text(res.text, limit)


# ---------------------------------------------------------------------- input, output
FIELDS = {"titre": str, "resume": str, "points_cles": list, "enjeux": list, "contexte": str, "a_surveiller": list}


def parse_brief(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise WriterError("no JSON object in the answer")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise WriterError(f"invalid JSON: {exc.msg}") from exc
    out = {}
    for key, kind in FIELDS.items():
        v = data.get(key)
        if kind is str:
            if not isinstance(v, str) or not v.strip():
                raise WriterError(f"missing field {key}")
            out[key] = v.strip()
        else:
            if isinstance(v, str):
                v = [v]
            if not isinstance(v, list):
                raise WriterError(f"field {key} is not a list")
            out[key] = [str(x).strip() for x in v if str(x).strip()][:5]
    out["titre"] = out["titre"][:140]
    out["sources_limitees"] = bool(data.get("sources_limitees", False))
    return out


def _event_rows(con: sqlite3.Connection, cfg: RedactionConfig, only: list[str] | None, all_missing: bool = False):
    if only:
        q = ",".join("?" * len(only))
        return con.execute(f"SELECT * FROM events WHERE event_id IN ({q})", only).fetchall()
    if all_missing:
        return con.execute("SELECT * FROM events WHERE importance >= ? ORDER BY importance DESC, first_seen DESC",
                           (cfg.min_importance,)).fetchall()
    return con.execute("SELECT * FROM events WHERE importance >= ? ORDER BY importance DESC, first_seen DESC LIMIT ?",
                       (cfg.min_importance, cfg.max_events_per_run * 3)).fetchall()


def _heads(con: sqlite3.Connection, event_id: str):
    return con.execute(
        "SELECT i.*, s.source_name FROM event_items ei JOIN items i ON i.id = ei.item_id "
        "LEFT JOIN sources s ON s.source_id = i.source_id WHERE ei.event_id = ? AND ei.is_head = 1 "
        "ORDER BY i.tier, i.published_at", (event_id,)).fetchall()


def brief_key(event_id: str, head_ids: list[str], model: str) -> str:
    raw = "|".join([PROMPT_VERSION, model, event_id, *sorted(head_ids)])
    return "br-" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def build_prompt(ev: sqlite3.Row, heads: list, pages: dict[str, str], cfg: RedactionConfig) -> str:
    parts = [f"Événement détecté le {ev['first_seen'][:10]}. Type estimé : {ev['event_type']}.",
             f"Nombre d'origines indépendantes : {len(heads)}.", ""]
    for n, h in enumerate(heads[:4], start=1):
        body = pages.get(h["id"]) or h["cleaned_content"] or h["summary"] or ""
        parts += [f"--- Source {n} : {h['source_name'] or h['source_id']}"
                  + (f" (via {h['publisher']})" if h["publisher"] and h["publisher"] != h["source_name"] else ""),
                  f"Date : {h['published_at'][:10]}  Langue : {h['language']}",
                  f"Titre : {h['title']}",
                  f"Texte : {body[: cfg.max_chars_per_source] if body else '(titre seulement)'}", ""]
    parts.append("Rédige la fiche JSON.")
    return "\n".join(parts)


def run_redaction(db_path: Path, cfg: RedactionConfig | None = None, *, only: list[str] | None = None,
                  force: bool = False, writer: Writer | None = None, fetch=None, log=print,
                  all_missing: bool = False, progress=None, should_stop=None) -> dict:
    """Write briefs for the events that have none for their current set of origins.

    Default: the most important ones, at most ``max_events_per_run``. ``all_missing``: every
    event of the window, most important first. ``progress(done, total, title)`` is called after
    each event; ``should_stop()`` is checked between two events.
    """
    cfg = cfg or RedactionConfig.load()
    writer = writer or make_writer(cfg)
    stats = {"selected": 0, "cached": 0, "pending": 0, "written": 0, "failed": 0, "pages": 0, "stopped": False}
    if writer is None:
        log("redaction disabled (provider = none in config/pipeline/redaction.toml)")
        return stats
    if fetch is None and cfg.fetch_articles:
        fetch = PageFetcher()
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        pending = []
        for ev in _event_rows(con, cfg, only, all_missing):
            heads = _heads(con, ev["event_id"])
            if not heads:
                continue
            stats["selected"] += 1
            key = brief_key(ev["event_id"], [h["id"] for h in heads], writer.model)
            if not force and con.execute("SELECT 1 FROM event_briefs WHERE brief_key = ?", (key,)).fetchone():
                stats["cached"] += 1
                continue
            pending.append((ev, heads, key))
        if not only and not all_missing:
            pending = pending[: cfg.max_events_per_run]
        stats["pending"] = len(pending)
        if progress:
            progress(0, len(pending), "")
        failures_in_a_row = 0
        for n, (ev, heads, key) in enumerate(pending, start=1):
            if should_stop and should_stop():
                stats["stopped"] = True
                log("stopped on request")
                break
            pages = {}
            if fetch:
                for h in heads[:4]:
                    txt = fetch(h["url"], cfg.max_chars_per_source)
                    if txt and len(txt) > len(h["cleaned_content"] or ""):
                        pages[h["id"]] = txt
                stats["pages"] += len(pages)
            try:
                if hasattr(writer, "brief"):          # local: extractive summary + translation, no prompt
                    brief = writer.brief(ev, heads, pages)
                else:
                    brief = parse_brief(writer.complete(SYSTEM, build_prompt(ev, heads, pages, cfg)))
            except Exception as exc:  # noqa: BLE001 - one event must not stop the others
                exc = exc if isinstance(exc, WriterError) else WriterError(f"{exc.__class__.__name__}: {exc}")
                stats["failed"] += 1
                failures_in_a_row += 1
                log(f"  {ev['event_id']}: {exc}")
                if progress:
                    progress(n, len(pending), "")
                if failures_in_a_row >= 3:
                    log("three failures in a row: redaction stopped")
                    break
                continue
            failures_in_a_row = 0
            with con:
                con.execute("INSERT OR REPLACE INTO event_briefs VALUES (?,?,?,?,?,?,?,?,?)",
                            (key, ev["event_id"], datetime.now(UTC).isoformat(timespec="seconds"), writer.name,
                             writer.model, PROMPT_VERSION, len(heads), len(pages), json.dumps(brief, ensure_ascii=False)))
            stats["written"] += 1
            log(f"  {ev['event_id']}: {brief['titre'][:90]}")
            if progress:
                progress(n, len(pending), brief["titre"])
    finally:
        con.close()
    return stats
