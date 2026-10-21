"""GDELT DOC 2.0 API connector (``mode=artlist&format=json``, no key).

GDELT returns article metadata only (no text): url, title, seendate, domain,
language, source country. The real publisher is ``domain``; it is kept in
``extra`` so later stages can tell the aggregator from the outlet. On errors
GDELT answers with plain text, which becomes a ParseError.
"""

from __future__ import annotations

import json

from app.pipeline.config import SourceConfig
from app.pipeline.connectors.base import Connector, ParseError, Payload
from app.pipeline.models import RawItem


class GdeltConnector(Connector):
    name = "gdelt"

    def parse(self, payload: Payload, source: SourceConfig):
        text = payload.body.decode("utf-8", errors="replace").strip()
        if not text:
            raise ParseError("empty payload")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ParseError(f"not JSON ({text[:60]!r}…): {exc.msg}") from exc
        if data == {}:
            return [], []            # GDELT answers {} when nothing matches: an empty feed, not an error
        articles = data.get("articles") if isinstance(data, dict) else None
        if articles is None:
            raise ParseError("no 'articles' key")
        items, warnings = [], []
        for i, art in enumerate(articles):
            if not isinstance(art, dict):
                warnings.append(("ENTRY_INVALID", f"article #{i} is not an object"))
                continue
            items.append(RawItem(
                source=source.id,
                source_type=source.source_type,
                url=art.get("url", ""),
                title=art.get("title", ""),
                summary="",
                published_at=art.get("seendate", ""),
                author="",
                language=art.get("language", ""),
                raw_content=json.dumps(art, ensure_ascii=False, sort_keys=True),
                extra={"publisher": art.get("domain", ""), "source_country": art.get("sourcecountry", ""),
                       "url_mobile": art.get("url_mobile", ""), "metadata_only": True},
            ))
        return items, warnings
