"""Connector registry. Adding a source type = one parser class + one line here."""

from __future__ import annotations

from app.pipeline.connectors.arxiv import ArxivConnector
from app.pipeline.connectors.base import Connector
from app.pipeline.connectors.gdelt import GdeltConnector
from app.pipeline.connectors.google_news import GoogleNewsConnector
from app.pipeline.connectors.hal import HalConnector
from app.pipeline.connectors.rss import RssConnector
from app.pipeline.connectors.youtube import YouTubeConnector

CONNECTORS: dict[str, type[Connector]] = {
    "rss": RssConnector,
    "google_news": GoogleNewsConnector,
    "gdelt": GdeltConnector,
    "youtube": YouTubeConnector,
    "arxiv": ArxivConnector,
    "hal": HalConnector,
}


def get_connector(name: str) -> Connector:
    return CONNECTORS[name]()
