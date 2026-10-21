"""Data API over the pipeline database (``python -m app.pipeline run``).

Every list endpoint is filtered and paginated (page_size ≤ 100): the API never
returns a whole table. Rows carry their scores, the explanation of each score,
their provenance and the versions that produced them. When the database was
built from test fixtures, every response says so (``data_origin`` + ``notice``).
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import pipeline_db
from app.pipeline.queries import MAX_PAGE_SIZE, DatabaseMissing, Page, Reader

router = APIRouter(prefix="/api", tags=["data"])


def reader() -> Iterator[Reader]:
    try:
        r = Reader(pipeline_db())
    except DatabaseMissing as exc:
        raise HTTPException(503, str(exc)) from exc
    try:
        yield r
    finally:
        r.close()


def paging(page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=MAX_PAGE_SIZE)) -> Page:
    return Page.of(page, page_size)


@router.get("/items")
def items(
    since: str | None = Query(None, description="ISO date or datetime (published_at ≥)"),
    until: str | None = None, sector: str | None = None, topic: str | None = None, source: str | None = None,
    company: str | None = Query(None, description="entity id, e.g. mbda"), country: str | None = Query(None, description="ISO-2"),
    technology: str | None = None, programme: str | None = None, event_type: str | None = None, language: str | None = None,
    min_importance: int | None = Query(None, ge=0, le=100), min_credibility: int | None = Query(None, ge=0, le=100),
    min_novelty: int | None = Query(None, ge=0, le=100),
    status: str = Query("processed", pattern="^(processed|out_of_scope|all)$"),
    include_duplicates: bool = Query(False, description="also return copies (non-head items of a duplicate group)"),
    q: str | None = None, sort: str = Query("published", pattern="^(published|importance|credibility|novelty|quality)$"),
    page: Page = Depends(paging), r: Reader = Depends(reader),
):
    return r.items(since=since, until=until, sector=sector, topic=topic, source=source, company=company, country=country,
                   technology=technology, programme=programme, event_type=event_type, language=language,
                   min_importance=min_importance, min_credibility=min_credibility, min_novelty=min_novelty, status=status,
                   include_duplicates=include_duplicates, q=q, sort=sort, page=page)


@router.get("/items/{item_id}")
def item(item_id: str, r: Reader = Depends(reader)):
    out = r.item(item_id)
    if out is None:
        raise HTTPException(404, "Item not found")
    return out


@router.get("/events")
def events(
    since: str | None = Query(None, description="first_seen ≥"), until: str | None = None, sector: str | None = None,
    topic: str | None = None, source: str | None = None, company: str | None = None, country: str | None = None,
    technology: str | None = None, programme: str | None = None,
    status: str | None = Query(None, pattern="^(confirmed|candidate|contested)$"),
    confidence: str | None = Query(None, pattern="^(low|moderate|high)$"), event_type: str | None = None,
    min_importance: int | None = Query(None, ge=0, le=100), min_credibility: int | None = Query(None, ge=0, le=100),
    min_novelty: int | None = Query(None, ge=0, le=100), q: str | None = None,
    sort: str = Query("importance", pattern="^(importance|recent|credibility|novelty|strategic)$"),
    page: Page = Depends(paging), r: Reader = Depends(reader),
):
    return r.events(since=since, until=until, sector=sector, topic=topic, source=source, company=company, country=country,
                    technology=technology, programme=programme, status=status, confidence=confidence, event_type=event_type,
                    min_importance=min_importance, min_credibility=min_credibility, min_novelty=min_novelty, q=q, sort=sort,
                    page=page)


@router.get("/events/{event_id}")
def event(event_id: str, r: Reader = Depends(reader)):
    out = r.event(event_id)
    if out is None:
        raise HTTPException(404, "Event not found")
    return out


@router.get("/signals")
def signals(
    kind: str | None = Query(None, pattern="^(acceleration|surge|drop|new_entity|new_relation)$"),
    subject_type: str | None = None, subject: str | None = Query(None, description="entity id, ISO country or taxonomy label"),
    min_confidence: int | None = Query(None, ge=0, le=100), level: str | None = Query(None, pattern="^(low|moderate|high)$"),
    window_end: str | None = Query(None, description="as-of time of a past run; default: latest"),
    page: Page = Depends(paging), r: Reader = Depends(reader),
):
    return r.signals(kind=kind, subject_type=subject_type, subject=subject, min_confidence=min_confidence, level=level,
                     window_end=window_end, page=page)


@router.get("/entities")
def entities(q: str | None = None, type: str | None = None, country: str | None = None, sector: str | None = None,
             min_events: int | None = Query(None, ge=0), page: Page = Depends(paging), r: Reader = Depends(reader)):
    return r.entities(q=q, type=type, country=country, sector=sector, min_events=min_events, page=page)


@router.get("/entities/{entity_id}")
def entity(entity_id: str, r: Reader = Depends(reader)):
    out = r.entity(entity_id)
    if out is None:
        raise HTTPException(404, "Entity not found")
    return out


@router.get("/sources")
def sources(r: Reader = Depends(reader)):
    return r.source_list()


@router.get("/topics")
def topics(r: Reader = Depends(reader)):
    return r.topics()


@router.get("/metrics")
def metrics(r: Reader = Depends(reader)):
    return r.metrics()


@router.get("/pipeline/status")
def pipeline_status(r: Reader = Depends(reader)):
    return r.status()


@router.get("/pipeline/runs")
def pipeline_runs(page: Page = Depends(paging), r: Reader = Depends(reader)):
    return r.runs(page)


@router.get("/pipeline/runs/{run_id}")
def pipeline_run(run_id: str, level: str | None = Query(None, pattern="^(info|warning|error)$"),
                 page: Page = Depends(paging), r: Reader = Depends(reader)):
    out = r.run(run_id, level, page)
    if out is None:
        raise HTTPException(404, "Run not found")
    return out
