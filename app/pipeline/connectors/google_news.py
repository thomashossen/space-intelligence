"""Google News RSS connector (search feeds, no key).

Google News titles end with " - Publisher" and links point to news.google.com
redirect pages. The connector splits the publisher out of the title and keeps it
in ``extra['publisher']``; it does NOT decode or follow the redirect link (it
needs JavaScript and is not meant for automated decoding). Deduplication against
the publisher's own feed therefore relies on titles (level 2), not URLs.
"""

from __future__ import annotations

from app.pipeline.config import SourceConfig
from app.pipeline.connectors.rss import RssConnector
from app.pipeline.models import RawItem


class GoogleNewsConnector(RssConnector):
    name = "google_news"

    def entry_to_item(self, entry, feed_lang: str, source: SourceConfig) -> RawItem | None:
        item = super().entry_to_item(entry, feed_lang, source)
        if item is None:
            return None
        src = entry.get("source") or {}
        publisher = src.get("title", "") if hasattr(src, "get") else ""
        title = item.title
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3]
        elif " - " in title and not publisher:
            title, publisher = title.rsplit(" - ", 1)
        item.title = title.strip()
        item.extra.update({"publisher": publisher.strip(), "publisher_url": src.get("href", "") if hasattr(src, "get") else ""})
        # The summary of a Google News item is a list of links, not content.
        item.extra["summary_is_link_list"] = True
        return item
