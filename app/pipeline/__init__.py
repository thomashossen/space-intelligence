"""Collection and processing pipeline.

COLLECT → NORMALISE → CLEAN → DEDUPLICATE → ENRICH → CLASSIFY → CLUSTER (events)
→ SCORE → RELATIONS → SIGNALS → STORE, then the JSON API serves what was stored.

Run it with ``python -m app.pipeline run --fixtures`` (see ``__main__.py``).
Business rules live in ``config/pipeline/*.toml``; this package holds the mechanics.
"""

PROCESSING_VERSION = "1.0.0"
