"""Application settings.

The site and the JSON API read the live pipeline database
(``<data dir>/pipeline/live.sqlite``, written by ``python -m app.pipeline run --live`` or the
"Actualiser" button). SECTOR_INTEL_PIPELINE_DB points them at another file (tests do).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Europe/Paris")
# Where the live database and the translation model live. On a host whose code folder is
# wiped at each deploy (Render), point it at a persistent disk: SECTOR_INTEL_DATA_DIR=/var/data
DATA_DIR = Path(os.getenv("SECTOR_INTEL_DATA_DIR") or BASE_DIR / "data")
LIVE_DB = DATA_DIR / "pipeline" / "live.sqlite"


def pipeline_db() -> Path:
    """Database read by the site and the API, resolved on every request so that a first
    collection is picked up without restarting the server."""
    env = os.getenv("SECTOR_INTEL_PIPELINE_DB")
    return Path(env) if env else LIVE_DB


@dataclass(frozen=True)
class Settings:
    app_name: str = "Space Intelligence"
    timezone: ZoneInfo = TZ
    templates_dir: Path = BASE_DIR / "templates"
    static_dir: Path = BASE_DIR / "static"


settings = Settings()
