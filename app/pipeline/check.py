"""`python -m app.pipeline check`: fetch every live source once and report what it returns.

Nothing is stored. With --disable-failing, sources that answer with an error
(HTTP error, timeout, unreadable payload) get ``enabled = false`` in the registry,
so the next runs stay clean. A source that answers but has no entry is kept.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.pipeline.config import CONFIG_DIR, SourceConfig, load_config
from app.pipeline.connectors import get_connector
from app.pipeline.connectors.base import HttpTransport
from app.pipeline.dates import parse_date

UTC = timezone.utc


def check_sources(sources_file: str = "sources.live.toml", only: list[str] | None = None,
                  include_disabled: bool = False, transport=None) -> list[dict]:
    cfg = load_config(sources_file)
    transport = transport or HttpTransport()
    now = datetime.now(UTC)
    rows = []
    for src in cfg.sources:
        if only and src.id not in only:
            continue
        if not src.enabled and not include_disabled and not only:
            continue
        if "REPLACE" in src.url:
            continue
        res = get_connector(src.connector).collect(src, transport)
        dates = [parse_date(i.published_at, collected_at=now, as_of=now)[0] for i in res.items]
        rows.append({
            "id": src.id, "enabled": src.enabled, "status": res.status, "entries": len(res.items),
            "last_7d": sum((now - d).days < 7 for d in dates),
            "newest": max(dates).strftime("%Y-%m-%d") if dates else "-",
            "message": (res.errors or res.warnings or [("", "")])[0][1][:90],
            "seconds": res.seconds,
        })
    return rows


def disable(source_ids: list[str], sources_file: str = "sources.live.toml") -> list[str]:
    """Set enabled = false on the given sources, keeping the file's comments and layout."""
    path = CONFIG_DIR / sources_file
    text = path.read_text(encoding="utf-8")
    parts = text.split("[[source]]")
    changed = []
    for i, block in enumerate(parts[1:], start=1):
        m = re.search(r'^id\s*=\s*"([^"]+)"', block, re.M)
        if m and m.group(1) in source_ids and re.search(r"^enabled\s*=\s*true", block, re.M):
            parts[i] = re.sub(r"^enabled\s*=\s*true", "enabled = false   # disabled by `check`", block, count=1, flags=re.M)
            changed.append(m.group(1))
    path.write_text("[[source]]".join(parts), encoding="utf-8")
    return changed


def print_report(rows: list[dict]) -> None:
    print(f"{'source':<24}{'status':<9}{'entries':>8}{'7 days':>8}  {'newest':<11} message")
    for r in rows:
        print(f"{r['id']:<24}{r['status']:<9}{r['entries']:>8}{r['last_7d']:>8}  {r['newest']:<11} {r['message']}")
    ok = sum(r["status"] != "error" for r in rows)
    print(f"\n{ok}/{len(rows)} sources answer; {sum(r['last_7d'] for r in rows)} entries from the last 7 days.")


def reenable(source_ids: list[str], sources_file: str = "sources.live.toml") -> list[str]:
    """Undo `disable`: only sources that `check` itself switched off are turned back on."""
    path = CONFIG_DIR / sources_file
    text = path.read_text(encoding="utf-8")
    parts = text.split("[[source]]")
    changed = []
    for i, block in enumerate(parts[1:], start=1):
        m = re.search(r'^id\s*=\s*"([^"]+)"', block, re.M)
        if m and m.group(1) in source_ids and "# disabled by `check`" in block:
            parts[i] = re.sub(r"^enabled\s*=\s*false\s*# disabled by `check`", "enabled = true", block, count=1, flags=re.M)
            changed.append(m.group(1))
    path.write_text("[[source]]".join(parts), encoding="utf-8")
    return changed


def disabled_by_check(sources_file: str = "sources.live.toml") -> list[str]:
    text = (CONFIG_DIR / sources_file).read_text(encoding="utf-8")
    out = []
    for block in text.split("[[source]]")[1:]:
        m = re.search(r'^id\s*=\s*"([^"]+)"', block, re.M)
        if m and "# disabled by `check`" in block:
            out.append(m.group(1))
    return out
