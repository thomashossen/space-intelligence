"""Public site: home, news, event detail, trends and actors, read from the pipeline database."""

from __future__ import annotations

from urllib.parse import urlencode

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.services import site
from app.templating import templates

router = APIRouter()


def _render(request: Request, name: str, active: str, ctx: dict, status_code: int = 200):
    return templates.TemplateResponse(request, f"site/{name}.html", {"active": active, **ctx}, status_code=status_code)


def _with_reader(request: Request, active: str, build):
    reader = site.open_reader()
    if reader is None:
        return _render(request, "missing", active, {"info": None, "db": str(site.pipeline_db()),
                                                    "refresh_allowed": bool(_refresh_allowed(request))}, 503)
    try:
        return build(reader)
    finally:
        reader.close()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request):
    return _with_reader(request, "accueil", lambda r: _render(request, "home", "accueil", site.home(r)))


@router.get("/actualites", response_class=HTMLResponse, include_in_schema=False)
def news(request: Request, q: str = "", scope: str = "", type: str = "", theme: str = "", sort: str = "recent",
         page: int = 1):
    q, scope = q.strip()[:120], scope if scope in site.SCOPES else ""
    kind = type if type in site.EVENT_TYPES else ""
    theme = theme.upper() if theme.upper() in site.LABELS else ""

    def build(r):
        ctx = site.news(r, q=q, scope=scope, kind=kind, theme=theme, sort=sort, page=page)
        params = {k: v for k, v in {"q": q, "scope": scope, "type": kind, "theme": theme,
                                     "sort": sort if sort == "importance" else ""}.items() if v}
        ctx["prev_url"] = "?" + urlencode({**params, "page": ctx["page"] - 1}) if ctx["page"] > 1 else ""
        ctx["next_url"] = "?" + urlencode({**params, "page": ctx["page"] + 1}) if ctx["page"] < ctx["pages"] else ""
        return _render(request, "news", "actualites", ctx | {"q": q, "scope": scope, "kind": kind, "theme": theme, "sort": sort})
    return _with_reader(request, "actualites", build)


@router.get("/evenements/{event_id}", response_class=HTMLResponse, include_in_schema=False)
def event(request: Request, event_id: str):
    def build(r):
        ctx = site.event(r, event_id)
        if ctx is None:
            raise HTTPException(404, "Événement introuvable")
        return _render(request, "event", "actualites", ctx)
    return _with_reader(request, "actualites", build)


@router.get("/tendances", response_class=HTMLResponse, include_in_schema=False)
def trends(request: Request):
    return _with_reader(request, "tendances", lambda r: _render(request, "trends", "tendances", site.trends(r)))


@router.get("/acteurs", response_class=HTMLResponse, include_in_schema=False)
def actors(request: Request, q: str = "", type: str = "", sort: str = "cited"):
    q = q.strip()[:80]
    type_ = type if type in {t for t, _, _ in site.ENTITY_TYPES} else ""
    sort = sort if sort in ("cited", "rising", "recent", "name") else "cited"
    return _with_reader(request, "acteurs", lambda r: _render(
        request, "actors", "acteurs", site.actors(r, q=q, type_=type_, sort=sort) | {"q": q, "type_": type_}))


@router.get("/acteurs/{entity_id}", response_class=HTMLResponse, include_in_schema=False)
def actor(request: Request, entity_id: str, page: int = 1):
    def build(r):
        ctx = site.actor(r, entity_id, page)
        if ctx is None:
            raise HTTPException(404, "Acteur introuvable")
        ctx["prev_url"] = f"?page={ctx['page'] - 1}" if ctx["page"] > 1 else ""
        ctx["next_url"] = f"?page={ctx['page'] + 1}" if ctx["page"] < ctx["pages"] else ""
        return _render(request, "actor", "acteurs", ctx)
    return _with_reader(request, "acteurs", build)


@router.get("/healthz", include_in_schema=False)
def healthz():
    """Liveness probe for the host (Render): the web process answers, with or without a database."""
    return {"ok": True}


@router.get("/collecte", response_class=HTMLResponse, include_in_schema=False)
def collecte(request: Request):
    return _with_reader(request, "collecte", lambda r: _render(request, "collecte", "collecte", site.collecte(r)))


# Addresses of the first prototype, kept working for bookmarks (permanent redirects).
LEGACY = {
    "/brief": "/", "/feed": "/actualites", "/events": "/actualites", "/search": "/actualites",
    "/signals": "/tendances", "/trends": "/tendances", "/scenarios": "/tendances",
    "/actors": "/acteurs", "/programs": "/acteurs?type=programme", "/technologies": "/acteurs?type=technology",
    "/geopolitics": "/actualites?theme=GEOPOLITICS", "/supply-chain": "/actualites?theme=SUPPLY_CHAIN",
    "/pipeline": "/collecte", "/sources": "/collecte",
    "/watchlists": "/", "/alerts": "/", "/settings": "/", "/design": "/",
}


def _legacy(target: str):
    def redirect():
        return RedirectResponse(target, status_code=301)
    return redirect


for _old, _new in LEGACY.items():
    router.add_api_route(_old, _legacy(_new), methods=["GET"], include_in_schema=False)


@router.get("/conditions", response_class=HTMLResponse, include_in_schema=False)
def conditions(request: Request):
    reader = site.open_reader()
    info = None
    if reader is not None:
        try:
            info = site.collection(reader)
        finally:
            reader.close()
    return _render(request, "conditions", "conditions", {"info": info})


# ---------------------------------------------------------------------- "Actualiser"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _refresh_allowed(request: Request) -> bool:
    """Collecting hits external sites: only the machine running the server may trigger it,
    unless SECTOR_INTEL_REFRESH=open (e.g. behind your own authentication)."""
    return os.getenv("SECTOR_INTEL_REFRESH") == "open" or (request.client and request.client.host in LOCAL_HOSTS)


@router.get("/api/refresh", tags=["refresh"])
def refresh_status(request: Request):
    """State of the background refresh (collect + process + briefs)."""
    from app.services.refresh import job  # noqa: PLC0415

    local = bool(request.client and request.client.host in LOCAL_HOSTS)
    return {**job.status(), "allowed": bool(_refresh_allowed(request)), "can_stop": local}


@router.post("/api/refresh", tags=["refresh"])
def refresh_start(request: Request):
    """Start a background refresh: re-test disabled sources, run the live pipeline, write briefs."""
    from app.services.refresh import job  # noqa: PLC0415

    if not _refresh_allowed(request):
        return JSONResponse({"state": "forbidden", "message": "L'actualisation ne peut être lancée que depuis la machine qui héberge le site."},
                            status_code=403)
    state = job.start()
    return JSONResponse(state, status_code=202 if state["state"] == "running" else 200)


@router.post("/api/refresh/stop", tags=["refresh"])
def refresh_stop(request: Request):
    """Stop the running refresh after the current step (briefs stop between two events)."""
    from app.services.refresh import job  # noqa: PLC0415

    if not (request.client and request.client.host in LOCAL_HOSTS):
        # On a public site a visitor must not be able to stop someone else's refresh.
        return JSONResponse({"state": "forbidden", "message": "Seule la machine qui héberge le site peut arrêter une actualisation."},
                            status_code=403)
    return job.stop()
