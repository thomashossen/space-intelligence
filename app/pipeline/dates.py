"""Date parsing for feed and API formats. All outputs are timezone-aware UTC.

Never guesses silently: an unparseable or implausible date falls back to the
collection time and the item is flagged ``date_estimated`` (with the reason), so
the credibility score's temporal-consistency component and the data-quality
score both see it.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

UTC = timezone.utc
MIN_YEAR = 1990
FUTURE_TOLERANCE = timedelta(hours=2)

MONTHS = {
    "jan": 1, "january": 1, "janvier": 1, "feb": 2, "february": 2, "février": 2, "fevrier": 2,
    "mar": 3, "march": 3, "mars": 3, "apr": 4, "april": 4, "avril": 4, "may": 5, "mai": 5,
    "jun": 6, "june": 6, "juin": 6, "jul": 7, "july": 7, "juillet": 7, "aug": 8, "august": 8,
    "août": 8, "aout": 8, "sep": 9, "sept": 9, "september": 9, "septembre": 9, "oct": 10,
    "october": 10, "octobre": 10, "nov": 11, "november": 11, "novembre": 11, "dec": 12,
    "december": 12, "décembre": 12, "decembre": 12,
}


def _aware(dt: datetime, flags: list[str]) -> datetime:
    if dt.tzinfo is None:
        flags.append("timezone_assumed_utc")
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _try_formats(text: str, flags: list[str]) -> datetime | None:
    s = text.strip()
    # GDELT seendate: 20261006T141500Z
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z", s)
    if m:
        return datetime(*map(int, m.groups()), tzinfo=UTC)
    # ISO 8601 (Atom, HAL, arXiv): 2026-10-06T14:15:00Z, 2026-10-06 00:00:00, 2026-10-06
    iso = s.replace(" ", "T", 1) if re.match(r"^\d{4}-\d{2}-\d{2} \d", s) else s
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            flags.append("date_only")
        return _aware(dt, flags)
    except ValueError:
        pass
    # RFC 822 (RSS): Tue, 06 Oct 2026 14:15:00 +0200
    try:
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return _aware(dt, flags)
    except (TypeError, ValueError, IndexError):
        pass
    # 06/10/2026 (day first, European feeds)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        d, mo, y = map(int, m.groups())
        flags.append("date_only")
        try:
            return datetime(y, mo, d, tzinfo=UTC)
        except ValueError:
            return None
    # 6 octobre 2026 / October 6, 2026
    low = s.lower().replace(",", " ")
    m = re.fullmatch(r"(\d{1,2})\s+([a-zéû]+)\.?\s+(\d{4})", low) or None
    if m:
        d, mon, y = m.groups()
    else:
        m = re.fullmatch(r"([a-zéû]+)\.?\s+(\d{1,2})\s+(\d{4})", low)
        if m:
            mon, d, y = m.groups()
    if m and mon in MONTHS:
        flags.append("date_only")
        try:
            return datetime(int(y), MONTHS[mon], int(d), tzinfo=UTC)
        except ValueError:
            return None
    return None


def parse_date(raw: str | None, *, collected_at: datetime, as_of: datetime | None = None) -> tuple[datetime, list[str]]:
    """Return (published_at UTC, flags). Falls back to ``collected_at`` with a flag."""
    flags: list[str] = []
    ref = as_of or collected_at
    if not raw or not str(raw).strip():
        return collected_at, ["date_missing", "date_estimated"]
    dt = _try_formats(str(raw), flags)
    if dt is None:
        return collected_at, ["date_invalid", "date_estimated"]
    if dt.year < MIN_YEAR:
        return collected_at, ["date_implausible", "date_estimated"]
    if dt > ref + FUTURE_TOLERANCE:
        return collected_at, ["date_future", "date_estimated"]
    return dt, flags


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text)
