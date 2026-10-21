"""RSS 2.0 / Atom connector (feedparser). Also used for institutional and industrial
newsrooms: a "institutional connector" is this one with tier-1 metadata in the registry.

feedparser runs with HTML sanitising and relative-URI resolution OFF so that
``raw_content`` is exactly what the publisher sent. Malformed XML that feedparser
can still read is accepted with an ``XML_MALFORMED`` warning; a payload with no
recoverable entry is a ParseError.
"""

from __future__ import annotations

import feedparser

from app.pipeline.config import SourceConfig
from app.pipeline.connectors.base import Connector, ParseError, Payload
from app.pipeline.models import RawItem


def _first(*values) -> str:
    for v in values:
        if v:
            return str(v)
    return ""


class RssConnector(Connector):
    name = "rss"

    def parse(self, payload: Payload, source: SourceConfig):
        warnings: list[tuple[str, str]] = []
        if not payload.body.strip():
            raise ParseError("empty payload")
        feed = feedparser.parse(payload.body, sanitize_html=False, resolve_relative_uris=False)
        if feed.bozo:
            exc = feed.get("bozo_exception")
            reason = f"{exc.__class__.__name__}: {exc}" if exc else "unknown"
            if not feed.entries:
                raise ParseError(f"not a readable feed ({reason})")
            if "CharacterEncoding" in exc.__class__.__name__:
                warnings.append(("ENCODING_OVERRIDE", f"{payload.url}: {reason}"))
            else:
                warnings.append(("XML_MALFORMED", f"{payload.url}: recovered {len(feed.entries)} entries ({reason})"))
        if not feed.get("version") and not feed.entries:
            # An HTML error page or any non-feed document: not "an empty feed".
            raise ParseError("payload is not an RSS/Atom feed (e.g. an HTML error page)")
        feed_lang = feed.feed.get("language", "") if feed.get("feed") else ""
        items = []
        for entry in feed.entries:
            item = self.entry_to_item(entry, feed_lang, source)
            if item is not None:
                items.append(item)
        return items, warnings

    def entry_to_item(self, entry, feed_lang: str, source: SourceConfig) -> RawItem | None:
        content = ""
        if entry.get("content"):
            content = entry.content[0].get("value", "")
        summary = entry.get("summary", "")
        lang = ""
        if entry.get("title_detail"):
            lang = entry.title_detail.get("language") or ""
        return RawItem(
            source=source.id,
            source_type=source.source_type,
            url=_first(entry.get("link"), entry.get("id") if str(entry.get("id", "")).startswith("http") else ""),
            title=entry.get("title", ""),
            summary=summary,
            # Short-circuit: reading "updated" when absent triggers a feedparser deprecation fallback.
            published_at=entry.get("published") or entry.get("dc_date") or entry.get("created") or entry.get("updated") or "",
            # feedparser keeps only the LAST author in entry.author when there are several.
            author=", ".join(a.get("name", "") for a in entry.get("authors", []) if a.get("name")) or entry.get("author", ""),
            language=_first(lang, feed_lang, ""),
            raw_content=content or summary,
            extra={"guid": entry.get("id", "")},
        )
