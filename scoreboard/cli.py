"""Command line. Run from the repository root:

    python -m scoreboard import-tickets EXPORT --client CLIENT --text-col COL [...] --out TICKETS
    python -m scoreboard validate TICKETS
    python -m scoreboard inspect-index --endpoint URL --index NAME [--client CLIENT] [--out CONFIG]
    python -m scoreboard prepare-labels TICKETS --out SHEET [--engine NAME --engine-config CONFIG]
    python -m scoreboard apply-labels SHEET --tickets TICKETS --out LABELED
    python -m scoreboard run LABELED --engine NAME [--engine-config CONFIG] [--runs 5]
    python -m scoreboard report RESULTS... [--max-wrong 5%] [--tickets LABELED]

Real tickets stay in clients-local/ (git-ignored): that is where results go by default.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import __version__
from .dataset import DatasetError, describe, file_sha256, load_tickets, save_tickets
from .engines import load_config, load_engine
from .engines.search_baseline import SearchError, describe_fields, fetch_index, suggest_config
from .importers import import_tickets
from .labeling import apply_labels, prepare_labels
from .metrics import summarize
from .pricing import load_prices
from .report import build_report, fmt_rate
from .runner import expand_paths, load_manifests, load_results, run_engine, safe_name, write_manifest

DEFAULT_RESULTS_DIR = "clients-local/scoreboard/results"


def say(message: str = "") -> None:
    print(message, file=sys.stderr)


def parse_rate(text: str) -> float:
    raw = text.strip()
    value = float(raw.rstrip("%").strip().replace(",", "."))
    if raw.endswith("%") or value > 1:
        value /= 100
    if not 0 < value < 1:
        raise argparse.ArgumentTypeError("expected a rate such as 5% or 0.05")
    return value


def every_tenth(index: int, total: int) -> bool:
    """Report progress at each 10% on big sets, only at the end on small ones."""
    if index + 1 == total:
        return True
    return total >= 20 and (index + 1) % (total // 10) == 0


def cmd_import(args) -> int:
    delimiter = "\t" if args.delimiter in ("tab", "\\t") else args.delimiter
    tickets, report = import_tickets(
        args.source,
        args.client,
        args.text_col,
        id_col=args.id_col,
        category_col=args.category_col,
        expected_col=args.expected_col,
        expected_transform=args.expected_transform,
        delimiter=delimiter,
        encoding=args.encoding,
        sheet=args.sheet,
        scrub=not args.no_scrub,
        scrub_patterns=args.scrub_pattern,
        limit=args.limit,
    )
    if not tickets:
        raise DatasetError("no ticket imported: check --text-col")
    save_tickets(args.out, tickets)
    source = report.source
    details = [source.get("format", "")]
    for key in ("encoding", "sheet"):
        if source.get(key):
            details.append(str(source[key]))
    if source.get("delimiter"):
        details.append(f"delimiter {source['delimiter']!r}")
    say(f"Read {report.rows} rows ({', '.join(details)}).")
    say(f"Imported {report.imported} tickets for {args.client} into {args.out}.")
    if report.labeled:
        say(f"{report.labeled} tickets already carry a label.")
    skipped = report.skipped_empty + report.skipped_no_id + report.duplicates
    if skipped:
        say(f"Skipped {report.skipped_empty} without text, {report.skipped_no_id} without id, {report.duplicates} duplicates.")
    if report.masked:
        say("Masked: " + ", ".join(f"{count} {label}" for label, count in sorted(report.masked.items())) + ".")
    if not args.no_scrub:
        say("Names of people are not masked: check a few tickets before sharing anything outside clients-local/.")
    return 0


def cmd_validate(args) -> int:
    tickets = load_tickets(args.tickets)
    info = describe(tickets)
    print(f"tickets        {info['tickets']}")
    print(f"labeled        {info['labeled']}  (with a fiche: {info['with_fiche']}, no fiche: {info['without_fiche']})")
    print(f"not labeled    {info['unlabeled']}")
    for client, count in info["by_client"].items():
        print(f"client {client:<12} {count}")
    if not info["labeled"]:
        say("No labeled ticket yet: use prepare-labels then apply-labels.")
    return 0


def cmd_inspect(args) -> int:
    index_def = fetch_index(args.endpoint, args.index, auth=args.auth, api_version=args.api_version)
    say(f"Fields of {args.index} (S searchable, R retrievable, F filterable, V vector):")
    for line in describe_fields(index_def):
        say("  " + line)
    config, notes = suggest_config(args.endpoint, index_def, args.client)
    text = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        say(f"Suggested engine config written to {args.out}.")
    else:
        print(text, end="")
    for note in notes:
        say(f"note: {note}")
    return 0


def cmd_prepare(args) -> int:
    tickets = load_tickets(args.tickets)
    engine = load_engine(args.engine, args.engine_config) if args.engine else None

    def progress(index, total):
        if every_tenth(index, total):
            say(f"  candidates {index + 1}/{total}")

    report = prepare_labels(tickets, args.out, engine=engine, k=args.k, include_labeled=args.all, progress=progress)
    say(f"Wrote {report.rows} rows to {args.out}.")
    if engine is not None:
        say(f"{report.with_candidates} rows have candidates from {engine.name}.")
        if report.candidate_errors:
            say(f"{report.candidate_errors} candidate lookups failed, e.g. {report.first_error}")
    say("In the 'expected' column type 1-5 (a candidate), a fiche id (several: id1|id2), or none.")
    return 0


def cmd_apply(args) -> int:
    tickets = load_tickets(args.tickets)
    updated, report = apply_labels(args.sheet, tickets)
    save_tickets(args.out, updated)
    say(
        f"{report.labeled} tickets labeled with a fiche, {report.none} with none, "
        f"{report.empty} rows left empty. Saved to {args.out}."
    )
    if report.unknown:
        say(f"{len(report.unknown)} rows match no ticket, e.g. {', '.join(report.unknown[:5])}")
    info = describe(updated)
    say(f"Labeled so far: {info['labeled']} of {info['tickets']}.")
    return 0


def cmd_run(args) -> int:
    tickets = load_tickets(args.tickets)
    if args.client:
        tickets = [t for t in tickets if t.client == args.client]
    labeled = [t for t in tickets if t.labeled]
    if args.limit:
        labeled = labeled[: args.limit]
    if not labeled:
        raise DatasetError("no labeled ticket to run (see prepare-labels / apply-labels)")
    config = load_config(args.engine_config)
    engine = load_engine(args.engine, config=config)
    out_dir = Path(args.out_dir)
    stem = safe_name(engine.name) + (f".{safe_name(args.client)}" if args.client else "")
    results_path = out_dir / f"{stem}.results.jsonl"
    started = dt.datetime.now(dt.timezone.utc)
    say(f"Running {engine.name} on {len(labeled)} tickets, {args.runs} run(s) -> {results_path}")

    def progress(run, index, total, record):
        if every_tenth(index, total):
            say(f"  [run {run + 1}/{args.runs}] {index + 1}/{total}")

    records = run_engine(engine, labeled, runs=args.runs, out_path=results_path, delay=args.delay, progress=progress)
    write_manifest(
        results_path,
        engine=engine.name,
        engine_spec=args.engine,
        engine_config=config,
        dataset=str(args.tickets),
        dataset_sha256=file_sha256(args.tickets),
        client=args.client,
        tickets=len(labeled),
        runs=args.runs,
        started_at=started.isoformat(timespec="seconds"),
        finished_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    summary = summarize(engine.name, records)
    say(
        f"Exact fiche @1 {fmt_rate(summary.exact)} · wrong fiche shown {fmt_rate(summary.wrong_shown)} · "
        f"errors {summary.errors}"
    )
    say(f"Next: python -m scoreboard report {results_path}")
    return 0


def cmd_report(args) -> int:
    paths = expand_paths(args.results)
    results = load_results(paths)
    if not results:
        raise DatasetError("the results files are empty")
    manifests = load_manifests(paths)
    prices = load_prices(args.prices)
    texts = {}
    if args.tickets:
        texts = {t.key: t.text for t in load_tickets(args.tickets)}
    markdown, summary = build_report(results, prices, args.max_wrong, manifests, tickets_text=texts)
    out = Path(args.out) if args.out else paths[0].parent / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    say(f"Report written to {out}.")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        say(f"Summary written to {args.json}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scoreboard",
        description="Replay labeled tickets through an engine and measure it.",
    )
    parser.add_argument("--version", action="version", version=f"scoreboard {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("import-tickets", help="turn an ITSM export (CSV, XLSX, JSON, JSONL) into a ticket file")
    p.add_argument("source", help="the export file")
    p.add_argument("--client", required=True, help="client name used everywhere else, e.g. client-s")
    p.add_argument("--text-col", action="append", required=True,
                   help="column holding the ticket text; repeat it to join several (subject, then description)")
    p.add_argument("--id-col", help="ticket number column (default: numbered in file order)")
    p.add_argument("--category-col")
    p.add_argument("--expected-col", help="column that already holds the right fiche, e.g. in a golden set")
    p.add_argument("--expected-transform", choices=["basename"], help="basename: keep 'KB1' from 'Kbs/KB1.pdf'")
    p.add_argument("--delimiter", help="CSV delimiter (default: detected); 'tab' for tabs")
    p.add_argument("--encoding", help="CSV encoding (default: UTF-8, then Windows-1252)")
    p.add_argument("--sheet", help="XLSX sheet name (default: the first)")
    p.add_argument("--no-scrub", action="store_true", help="keep e-mail addresses and phone numbers")
    p.add_argument("--scrub-pattern", action="append", default=[], help="extra regex to mask; repeatable")
    p.add_argument("--limit", type=int, help="import at most N tickets")
    p.add_argument("--out", required=True, help="ticket file to write (.jsonl)")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("validate", help="check a ticket file and count labels")
    p.add_argument("tickets")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("inspect-index", help="list an index's fields and suggest a search-baseline config")
    p.add_argument("--endpoint", required=True, help="https://<service>.search.windows.net")
    p.add_argument("--index", required=True)
    p.add_argument("--client", help="client name inside the index name, to write 'idx-{client}'")
    p.add_argument("--auth", choices=["entra", "key"], default="entra")
    p.add_argument("--api-version", default="2024-07-01")
    p.add_argument("--out", help="write the suggested config here instead of printing it")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("prepare-labels", help="write the labeling sheet, with candidates from an engine")
    p.add_argument("tickets")
    p.add_argument("--out", required=True, help="sheet to write: .xlsx (needs openpyxl) or .csv")
    p.add_argument("--engine", help="engine that proposes candidates, e.g. search-baseline")
    p.add_argument("--engine-config")
    p.add_argument("--k", type=int, default=5, choices=range(1, 11), metavar="1-10", help="candidates per ticket")
    p.add_argument("--all", action="store_true", help="include tickets that are already labeled")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("apply-labels", help="read the filled sheet back into the ticket file")
    p.add_argument("sheet")
    p.add_argument("--tickets", required=True, help="the ticket file the sheet was prepared from")
    p.add_argument("--out", required=True, help="labeled ticket file to write")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("run", help="replay the labeled tickets through an engine")
    p.add_argument("tickets")
    p.add_argument("--engine", required=True, help="search-baseline, fixture, or package.module:factory")
    p.add_argument("--engine-config")
    p.add_argument("--runs", type=int, default=5, help="repetitions, for the stability score (default 5)")
    p.add_argument("--client", help="only this client's tickets")
    p.add_argument("--limit", type=int, help="only the first N labeled tickets")
    p.add_argument("--delay", type=float, default=0.0, help="seconds between calls (rate limits)")
    p.add_argument("--out-dir", default=DEFAULT_RESULTS_DIR)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="write the scoreboard from results files")
    p.add_argument("results", nargs="+", help="*.results.jsonl files; globs are expanded")
    p.add_argument("--max-wrong", type=parse_rate, default=0.05, help="ceiling on wrong fiches shown (default 5%%)")
    p.add_argument("--prices", help="JSON price table (default: gpt-4o list prices in USD)")
    p.add_argument("--tickets", help="labeled ticket file, to show ticket text next to each miss")
    p.add_argument("--out", help="markdown file (default: report.md next to the first results file)")
    p.add_argument("--json", help="also write a JSON summary here")
    p.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args) or 0
    except (DatasetError, SearchError, ValueError, FileNotFoundError, ImportError) as exc:
        say(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        say("interrupted")
        return 130
