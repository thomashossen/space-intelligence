"""Command line.

    python -m app.pipeline run --fixtures --full-rebuild      # test fixtures (fictitious), from scratch
    python -m app.pipeline run --fixtures                     # incremental (default)
    python -m app.pipeline run --fixtures --source fx-esa     # one source
    python -m app.pipeline run --live --since 2026-10-01      # live registry (sources.live.toml)
    python -m app.pipeline status                             # last runs
    python -m app.pipeline sources                            # registry with tiers
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app.pipeline.config import DEFAULT_DB, LIVE_DB, ConfigError, default_db, load_config
from app.pipeline.runner import RunOptions, run_pipeline
from app.pipeline.store import connect


def _date(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _print_run(res) -> None:
    print(f"\nrun {res.run_id} — {res.status.upper()} in {res.total_seconds:.2f} s\n")
    print(f"{'stage':<18}{'seconds':>9}  counts")
    for s in res.stages:
        counts = {k: v for k, v in s.items() if k not in ("stage", "seconds", "errors") and v}
        print(f"{s['stage']:<18}{s['seconds']:>9.3f}  " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print("\nmetrics: " + json.dumps(res.metrics, indent=None))
    errors = [l for l in res.logs if l.level == "error"]
    if errors:
        print("\nerrors (isolated, the run went on):")
        for l in errors:
            print(f"  [{l.stage}] {l.source_id or '-'} {l.code}: {l.message.splitlines()[0][:140]}")


def _redact(db: Path, *, only=None, force=False, max_new=None, provider=None, model=None, all_missing=False) -> int:
    from app.pipeline.redaction import RedactionConfig, WriterError, run_redaction  # noqa: PLC0415

    cfg = RedactionConfig.load()
    cfg.provider = provider or cfg.provider
    cfg.model = model or cfg.model
    if max_new:
        cfg.max_events_per_run = max_new
    if cfg.provider in ("", "none"):
        if provider or only or force:
            print("redaction: provider is none. Set it in config/pipeline/redaction.toml or pass --provider.")
        return 0
    print(f"\nredaction ({cfg.provider}) — French briefs for the top events")
    try:
        st = run_redaction(db, cfg, only=only or None, force=force, log=lambda m: print(m), all_missing=all_missing,
                           progress=lambda n, total, title: print(f"[{n}/{total}]", end=" ") if n and total > 1 else None)
    except WriterError as exc:
        print(f"redaction skipped: {exc}", file=sys.stderr)
        return 1
    print(f"briefs: {st['written']} written, {st['cached']} already up to date, {st['failed']} failed, "
          f"{st['pages']} article pages read")
    return 0 if not st["failed"] or st["written"] else 1


def _list_models(provider: str | None, model: str | None) -> int:
    from app.pipeline.redaction import OpenAICompatWriter, RedactionConfig, WriterError, make_writer  # noqa: PLC0415

    cfg = RedactionConfig.load()
    cfg.provider = provider or cfg.provider
    cfg.model = model or cfg.model or "-"
    try:
        w = make_writer(cfg)
        if not isinstance(w, OpenAICompatWriter):
            print("--list-models works with groq, mistral and openai providers")
            return 1
        print("\n".join(w.models()))
    except WriterError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.pipeline", description="sector//intel collection and processing pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="collect and process")
    src = r.add_mutually_exclusive_group()
    src.add_argument("--fixtures", action="store_true", help="replay data/fixtures (default; fictitious data)")
    src.add_argument("--live", action="store_true", help="fetch the live registry (sources.live.toml)")
    mode = r.add_mutually_exclusive_group()
    mode.add_argument("--full-rebuild", action="store_true", help="drop derived data, then process everything again")
    mode.add_argument("--incremental", action="store_true", help="only new entries (default)")
    r.add_argument("--source", action="append", default=[], metavar="ID", help="collect only this source (repeatable)")
    r.add_argument("--since", type=_date, help="ignore entries published before this date (YYYY-MM-DD)")
    r.add_argument("--as-of", type=_date, help="reference time for windows and recency (default: fixture morning or now)")
    r.add_argument("--db", type=Path, default=None, help="default: data/pipeline/live.sqlite (--live) or sector_intel.sqlite")
    r.add_argument("--sources-file", help="registry file in config/pipeline/")
    st = sub.add_parser("status", help="last runs")
    st.add_argument("--db", type=Path, default=None)
    st.add_argument("--fixtures", action="store_true", help="show the fixtures database instead of the live one")
    st.add_argument("-n", type=int, default=5)
    rd = sub.add_parser("redact", help="write French briefs for the top events (language model)")
    rd.add_argument("--fixtures", action="store_true", help="use the fixtures database")
    rd.add_argument("--db", type=Path, default=None)
    rd.add_argument("--event", action="append", default=[], metavar="ID", help="only this event (repeatable)")
    rd.add_argument("--max", type=int, help="at most N new briefs")
    rd.add_argument("--all", action="store_true", help="every event without an up-to-date brief (can be long)")
    rd.add_argument("--force", action="store_true", help="rewrite even when a brief exists")
    rd.add_argument("--provider", help="none | local | groq | mistral | openai | anthropic | ollama (overrides redaction.toml)")
    rd.add_argument("--list-models", action="store_true", help="list the models the provider offers, then stop")
    rd.add_argument("--model", help="model name (overrides redaction.toml)")
    tr = sub.add_parser("translator", help="offline English→French model for provider = local")
    tr.add_argument("action", choices=("install", "test"))
    tr.add_argument("text", nargs="?", default="The European Space Agency signed a contract with Thales Alenia Space.")
    tr.add_argument("--if-missing", action="store_true", help="install: do nothing when the model is already there")
    ck = sub.add_parser("check", help="fetch each live source once and report (stores nothing)")
    ck.add_argument("--source", action="append", default=[], metavar="ID", help="check only this source (repeatable)")
    ck.add_argument("--all", action="store_true", help="include disabled sources")
    ck.add_argument("--disable-failing", action="store_true", help="set enabled = false on sources in error")
    ck.add_argument("--sources-file", default="sources.live.toml")
    so = sub.add_parser("sources", help="list the registry")
    so.add_argument("--live", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "run":
        opts = RunOptions(mode="live" if args.live else "fixtures", full_rebuild=args.full_rebuild, sources=args.source,
                          since=args.since, db_path=args.db or default_db("live" if args.live else "fixtures"), as_of=args.as_of, sources_file=args.sources_file)
        try:
            res = run_pipeline(opts)
        except ConfigError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 2
        _print_run(res)
        if args.live and res.status != "failed":
            _redact(default_db("live") if args.db is None else args.db)
        if res.status == "failed":
            print("\nRUN FAILED: nothing was collected (see errors above).", file=sys.stderr)
        return 0 if res.status in ("ok", "partial") else 1

    if args.cmd == "status":
        if args.db is None:
            args.db = DEFAULT_DB if args.fixtures or not LIVE_DB.exists() else LIVE_DB
        print(f"database: {args.db}")
        if not args.db.exists():
            print(f"no database at {args.db}. Run: python -m app.pipeline run --fixtures")
            return 1
        con = connect(args.db, readonly=True)
        for row in con.execute("SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?", (args.n,)):
            m = json.loads(row["metrics"] or "{}")
            print(f"{row['run_id']}  {row['status']:<8} {row['mode']:<13} {row['data_origin']:<8} "
                  f"{(row['total_seconds'] or 0):>6.2f}s  events={m.get('events', '-')} signals={m.get('signals', '-')} "
                  f"errors={m.get('errors', '-')}  rules={row['rules_version']}")
        return 0

    if args.cmd == "redact":
        if args.list_models:
            return _list_models(args.provider, args.model)
        db = args.db or default_db("fixtures" if args.fixtures else "live")
        if not db.exists():
            print(f"no database at {db}. Run: python -m app.pipeline run --live")
            return 1
        return _redact(db, only=args.event, force=args.force, max_new=args.max, provider=args.provider, model=args.model,
                       all_missing=args.all)

    if args.cmd == "translator":
        from app.pipeline import translate  # noqa: PLC0415

        try:
            if args.action == "install":
                installed = translate.model_dir().exists() and translate._package_root(translate.model_dir()) is not None
                if args.if_missing and installed:
                    print(f"translation model already in {translate.model_dir()}")
                    return 0
                translate.install()
            t0 = __import__("time").perf_counter()
            out = translate.Translator().translate([args.text])[0]
            print(f"{args.text}\n→ {out}\n({__import__('time').perf_counter() - t0:.2f} s)")
        except translate.TranslatorMissing as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0

    if args.cmd == "check":
        from app.pipeline.check import check_sources, disable, print_report  # noqa: PLC0415 - needs network

        rows = check_sources(args.sources_file, args.source, args.all)
        print_report(rows)
        if args.disable_failing:
            failing = [r["id"] for r in rows if r["status"] == "error" and r["enabled"]]
            changed = disable(failing, args.sources_file)
            print(f"disabled in {args.sources_file}: {', '.join(changed) or 'none'}")
        return 0

    if args.cmd == "sources":
        cfg = load_config("sources.live.toml" if args.live else "sources.toml")
        for s in cfg.sources:
            print(f"{s.id:<22} tier {s.tier}  {s.family}  {s.connector:<12} {'on ' if s.enabled else 'off'}  {s.name}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
