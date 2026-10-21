"""HAL search API connector (JSON, no key). Metadata and abstracts only."""

from __future__ import annotations

import json

from app.pipeline.config import SourceConfig
from app.pipeline.connectors.base import Connector, ParseError, Payload
from app.pipeline.models import RawItem


def _one(value) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


class HalConnector(Connector):
    name = "hal"

    def parse(self, payload: Payload, source: SourceConfig):
        try:
            data = json.loads(payload.body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ParseError(f"not JSON: {exc.msg}") from exc
        docs = (data.get("response") or {}).get("docs")
        if docs is None:
            raise ParseError("no response.docs")
        items = []
        for doc in docs:
            abstract = _one(doc.get("abstract_s"))
            authors = doc.get("authFullName_s") or []
            items.append(RawItem(
                source=source.id,
                source_type=source.source_type,
                url=doc.get("uri_s", ""),
                title=_one(doc.get("title_s")),
                summary=abstract,
                published_at=doc.get("submittedDate_tdate") or doc.get("producedDate_tdate", ""),
                author=", ".join(authors[:6]) + (" et al." if len(authors) > 6 else ""),
                language=_one(doc.get("language_s")),
                raw_content=json.dumps(doc, ensure_ascii=False, sort_keys=True),
                extra={"hal_id": doc.get("halId_s", ""), "produced": doc.get("producedDate_tdate", "")},
            ))
        return items, []
