"""Jinja environment shared by every page of the site."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from app.config import settings

templates = Jinja2Templates(directory=str(settings.templates_dir))
env = templates.env
env.trim_blocks = True
env.lstrip_blocks = True

# Bump to make browsers fetch a changed stylesheet or script.
SITE_VERSION = "11"

def _img_for(slot: str, key: str = "") -> str:
    from app.images import img_for  # noqa: PLC0415 - imported late so tests can point IMG_DIR elsewhere

    return img_for(slot, key)


env.globals.update(settings=settings, SITE_VERSION=SITE_VERSION, img_for=_img_for)
