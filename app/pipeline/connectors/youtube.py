"""YouTube channel feed connector (public Atom feed per channel, no API key).

Only the metadata in the feed is used: title, description, publication date.
No video download, no transcript scraping.
"""

from __future__ import annotations

from app.pipeline.config import SourceConfig
from app.pipeline.connectors.rss import RssConnector
from app.pipeline.models import RawItem


class YouTubeConnector(RssConnector):
    name = "youtube"

    def entry_to_item(self, entry, feed_lang: str, source: SourceConfig) -> RawItem | None:
        item = super().entry_to_item(entry, feed_lang, source)
        if item is None:
            return None
        description = entry.get("media_description") or entry.get("summary", "")
        item.summary = description
        item.raw_content = description
        item.extra.update({"video_id": entry.get("yt_videoid", ""), "channel_id": entry.get("yt_channelid", "")})
        return item
