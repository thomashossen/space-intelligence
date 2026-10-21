"""arXiv API connector (Atom). Free, no key; arXiv asks for ≥ 3 s between calls,
set with ``min_interval_s`` in the registry. Abstracts only, never PDFs.
"""

from __future__ import annotations

from app.pipeline.config import SourceConfig
from app.pipeline.connectors.rss import RssConnector
from app.pipeline.models import RawItem


class ArxivConnector(RssConnector):
    name = "arxiv"

    def entry_to_item(self, entry, feed_lang: str, source: SourceConfig) -> RawItem | None:
        item = super().entry_to_item(entry, feed_lang, source)
        if item is None:
            return None
        abs_url = next((l.get("href") for l in entry.get("links", []) if l.get("rel") == "alternate"), "") or entry.get("id", "")
        item.url = abs_url
        item.published_at = entry.get("published") or item.published_at
        item.extra.update({
            "arxiv_id": str(entry.get("id", "")).rsplit("/", 1)[-1],
            "primary_category": (entry.get("arxiv_primary_category") or {}).get("term", ""),
            "updated": entry.get("updated_parsed") and entry.get("updated", ""),
        })
        return item
