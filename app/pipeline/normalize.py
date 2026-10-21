"""NORMALISE + CLEAN: RawItem → Item.

Identity, canonical URL, dates, text cleaning, summary, language. Nothing here
decides relevance or importance. Rejections are rare and explicit: an entry with
neither title nor text cannot be analysed and is rejected with a logged reason;
everything else is kept and flagged.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from app.pipeline.config import PipelineConfig, SourceConfig
from app.pipeline.dates import iso, parse_date
from app.pipeline.models import Item, LogEntry, RawItem
from app.pipeline.text import (
    clean_inline, clean_text, detect_language, make_summary, normalize_language, sentences,
)
from app.pipeline.urls import canonicalize, domain_of


def _h(*parts: str, n: int = 14) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()[:n]


def raw_id(raw: RawItem) -> str:
    """Stable id of a delivered entry: same source + same fields = same raw item."""
    return "raw-" + _h(raw.source, raw.url, raw.title, raw.published_at, raw.summary[:200])


def item_id(source_id: str, url: str, title: str, published: str) -> str:
    key = f"{url}|{title}|{published}"
    return "it-" + _h(source_id, key, n=12)


def normalize(raw: RawItem, src: SourceConfig, cfg: PipelineConfig, *, collected_at: datetime,
              as_of: datetime, data_origin: str, source_url: str = "") -> tuple[Item | None, list[LogEntry]]:
    logs: list[LogEntry] = []
    flags: list[str] = []
    rid = raw_id(raw)

    canonical, url_flags = canonicalize(raw.url)
    published, date_flags = parse_date(raw.published_at, collected_at=collected_at, as_of=as_of)
    flags += date_flags
    for f in ("date_invalid", "date_future", "date_missing", "date_implausible"):
        if f in date_flags:
            logs.append(LogEntry("normalize", "warning", f.upper(),
                                 f"published_at {raw.published_at!r} → collection time used", src.id, rid))

    metadata_only = bool(raw.extra.get("metadata_only") or raw.extra.get("summary_is_link_list"))
    title_text, title_flags = clean_text(raw.title)
    title = " ".join(title_text.split())
    if "encoding_repaired" in title_flags:
        flags.append("encoding_repaired")
    if metadata_only:
        cleaned, summary_raw = "", ""
        flags.append("metadata_only")
    else:
        cleaned, clean_flags = clean_text(raw.raw_content)
        flags += [f for f in clean_flags if not (f == "empty_content" and raw.summary.strip())]
        summary_raw = clean_inline(raw.summary)
        if not cleaned and summary_raw:
            cleaned = summary_raw
    if not title:
        lead = sentences(cleaned or summary_raw)
        if lead:
            title = lead[0][:160]
            flags.append("title_derived")
            logs.append(LogEntry("normalize", "warning", "TITLE_DERIVED", "empty title: first sentence used", src.id, rid))
        else:
            logs.append(LogEntry("normalize", "warning", "REJECTED_EMPTY", "no title and no text: nothing to analyse", src.id, rid))
            return None, logs

    summary, summary_origin = make_summary(summary_raw, cleaned)
    flags.append(f"summary:{summary_origin}")
    if "encoding_repaired" in flags:
        logs.append(LogEntry("clean", "info", "ENCODING_REPAIRED", "mojibake repaired", src.id, rid))

    declared = normalize_language(raw.language or src.language)
    detected, conf = detect_language(f"{title}. {summary} {cleaned[:2000]}")
    language = detected
    if detected == "und" and declared and metadata_only:
        language, conf = declared, 0.5
        flags.append("language_declared")
    elif detected == "und":
        flags.append("language_unknown")
        logs.append(LogEntry("clean", "info", "LANGUAGE_UNKNOWN", f"language not detected (declared {declared or '—'})", src.id, rid))
    elif declared and declared != detected:
        flags.append(f"language_mismatch:{declared}")

    publisher = raw.extra.get("publisher", "") or ""
    if src.source_type != "aggregator":
        publisher = src.name
    now = iso(collected_at)
    item = Item(
        id=item_id(src.id, raw.url or canonical, title, iso(published)),
        raw_id=rid,
        source_id=src.id,
        source_type=src.source_type,
        family=src.family,
        tier=src.tier,
        source_url=source_url,
        url=raw.url,
        canonical_url=canonical,
        url_flags=url_flags,
        publisher=publisher or domain_of(raw.url),
        title=title,
        summary=summary,
        cleaned_content=cleaned,
        author=clean_inline(raw.author)[:200],
        language=language,
        language_declared=declared,
        language_confidence=conf,
        published_at=iso(published),
        collected_at=now,
        updated_at=now,
        flags=sorted(set(flags)),
        processing_version=cfg.processing_version,
        rules_version=cfg.rules_version,
        model_version=cfg.model_version,
        processed_at=now,
        data_origin=data_origin,
    )
    return item, logs
