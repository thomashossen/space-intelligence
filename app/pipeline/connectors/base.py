"""Connector contract and transports.

A connector turns one payload into ``RawItem`` objects. It holds NO business
logic: no scoring, no filtering on relevance, no deduplication. Transport is
separate so the same parser runs on live HTTP responses and on fixture files.

Failure isolation: ``Connector.collect`` never raises. Timeouts, unreachable
hosts, HTTP errors, malformed XML/JSON and empty feeds come back as a
``CollectResult`` with ``status`` and messages; the runner logs them and moves on
to the next source.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from app.pipeline.config import SourceConfig
from app.pipeline.models import RawItem

USER_AGENT = "sector-intel/1.0 (local research tool; respects robots and rate limits)"


class SourceUnavailable(Exception):
    """Network error, timeout, HTTP error or missing fixture."""


class ParseError(Exception):
    """The payload could not be parsed at all."""


@dataclass
class Payload:
    body: bytes
    url: str                     # where it came from (endpoint or fixture path)
    content_type: str = ""
    status: int = 200


@dataclass
class CollectResult:
    source_id: str
    items: list[RawItem] = field(default_factory=list)
    status: str = "ok"           # ok | warning | error
    errors: list[tuple[str, str]] = field(default_factory=list)     # (code, message)
    warnings: list[tuple[str, str]] = field(default_factory=list)
    endpoints: list[str] = field(default_factory=list)
    seconds: float = 0.0


class Transport(ABC):
    mode: str

    @abstractmethod
    def fetch(self, source: SourceConfig) -> list[Payload]:
        """Return one payload per endpoint. Raise SourceUnavailable on failure."""


class FixtureTransport(Transport):
    """Replays files from data/fixtures/feeds. A missing file = an unreachable source."""

    mode = "fixture"

    def __init__(self, root: Path):
        self.root = root

    def fetch(self, source: SourceConfig) -> list[Payload]:
        if not source.fixture:
            raise SourceUnavailable(f"no fixture configured for {source.id}")
        payloads = []
        for name in source.fixture:
            path = self.root / name
            if not path.exists():
                if name.startswith("history/"):
                    continue          # history files are optional
                raise SourceUnavailable(f"unreachable: {name} (simulated network failure)")
            payloads.append(Payload(path.read_bytes(), f"fixture://{name}"))
        return payloads


class HttpTransport(Transport):  # pragma: no cover - needs network
    """Live HTTP with timeout, one retry on 5xx/timeout, and a per-host minimum interval."""

    mode = "live"

    def __init__(self, timeout: float = 15.0):
        import httpx  # noqa: PLC0415 - only needed in live mode

        self._httpx = httpx
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=timeout)
        self._last: dict[str, float] = {}

    def _wait(self, source: SourceConfig) -> None:
        key = source.domain or source.id
        delay = source.min_interval_s - (time.monotonic() - self._last.get(key, 0.0))
        if delay > 0:
            time.sleep(delay)
        self._last[key] = time.monotonic()

    def fetch(self, source: SourceConfig) -> list[Payload]:
        if not source.url:
            raise SourceUnavailable(f"no URL configured for {source.id}")
        last_exc: Exception | None = None
        for attempt in range(2):
            self._wait(source)
            try:
                res = self.client.get(source.url, timeout=source.timeout_s)
            except self._httpx.TimeoutException:
                last_exc = SourceUnavailable(f"timeout after {source.timeout_s:.0f}s")
            except self._httpx.HTTPError as exc:
                last_exc = SourceUnavailable(f"network error: {exc.__class__.__name__}")
            else:
                if res.status_code == 429 and attempt == 0:
                    wait = res.headers.get("retry-after", "")
                    wait_s = min(float(wait), 60.0) if wait.replace(".", "", 1).isdigit() else 15.0
                    last_exc = SourceUnavailable(f"HTTP 429 (rate limited, retried after {wait_s:.0f}s)")
                    time.sleep(wait_s)
                    continue
                if res.status_code >= 500 and attempt == 0:
                    last_exc = SourceUnavailable(f"HTTP {res.status_code}")
                    time.sleep(2.0)
                    continue
                if res.status_code >= 400:
                    raise SourceUnavailable(f"HTTP {res.status_code}" + (" (rate limited twice)" if res.status_code == 429 else ""))
                return [Payload(res.content, str(res.url), res.headers.get("content-type", ""), res.status_code)]
            time.sleep(2.0)
        raise last_exc or SourceUnavailable("unknown network error")


class Connector(ABC):
    """Parse payloads into RawItems. Subclasses implement ``parse`` only."""

    name: str = "base"

    @abstractmethod
    def parse(self, payload: Payload, source: SourceConfig) -> tuple[list[RawItem], list[tuple[str, str]]]:
        """Return (items, warnings). Raise ParseError if nothing can be recovered."""

    def collect(self, source: SourceConfig, transport: Transport) -> CollectResult:
        t0 = time.perf_counter()
        result = CollectResult(source.id)
        try:
            payloads = transport.fetch(source)
        except SourceUnavailable as exc:
            result.status = "error"
            result.errors.append(("SOURCE_UNREACHABLE", str(exc)))
        except Exception as exc:  # noqa: BLE001 - one source must never stop the run
            result.status = "error"
            result.errors.append(("TRANSPORT_ERROR", f"{exc.__class__.__name__}: {exc}"))
        else:
            for payload in payloads:
                result.endpoints.append(payload.url)
                try:
                    items, warnings = self.parse(payload, source)
                except ParseError as exc:
                    result.errors.append(("PARSE_ERROR", f"{payload.url}: {exc}"))
                    continue
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(("PARSE_ERROR", f"{payload.url}: {exc.__class__.__name__}: {exc}"))
                    continue
                result.items.extend(items)
                result.warnings.extend(warnings)
            if result.errors:
                result.status = "error" if not result.items else "warning"
            elif result.warnings:
                result.status = "warning"
            if not result.items and not result.errors:
                result.warnings.append(("EMPTY_FEED", "the source answered but returned no entries"))
                result.status = "warning"
        result.seconds = round(time.perf_counter() - t0, 4)
        return result
