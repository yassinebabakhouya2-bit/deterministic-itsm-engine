"""Command line. Run from the repository root:

    python -m kecore import-fiches EXPORT --client CLIENT --id-col COL --body-col COL [...] --out FICHES.jsonl
    python -m kecore profile SOURCE --client CLIENT [--llm-config CONFIG]
    python -m kecore decompose SOURCE --client CLIENT [--llm-config CONFIG] [--replay | --refresh]
    python -m kecore show DECOMPOSED.jsonl FICHE_ID

SOURCE is a folder of fiches (.md, .txt, .html, and .docx/.pdf with python-docx/pypdf)
or a fiches .jsonl. Outputs go to clients-local/kecore/<client>/ (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .azure import AzureError
from .decompose import DecomposedFiche, Decomposer
from .errors import InputError
from .fiches import import_fiches, load_fiches, save_fiches
from .llm import AzureOpenAIChat, LLMError, RecordingLLM, load_llm_config
from .profile import Profile, build_profile
from .report import build_report

DEFAULT_OUT = Path("clients-local") / "kecore"
DEFAULT_CACHE = DEFAULT_OUT / "llm-cache"


def say(message: str = "") -> None:
    print(message, file=sys.stderr)


def every_tenth(index: int, total: int) -> bool:
    if index + 1 == total:
        return True
    return total >= 20 and (index + 1) % (total // 10) == 0


def make_llm(args):
    if not args.llm_config:
        return None
    config = load_llm_config(args.llm_config)
    inner = AzureOpenAIChat.from_config(config)
    mode = "replay" if args.replay else "refresh" if args.refresh else "record"
    return RecordingLLM(inner, args.cache_dir, mode=mode)


def llm_stats(llm, decomposer: Decomposer | None = None) -> dict:
    if llm is None:
        return {}
    stats = {"model": llm.model_id, "mode": llm.mode, "calls": llm.calls, "cached": llm.hits}
    if decomposer is not None:
        stats.update(errors=decomposer.llm_errors, **asdict(decomposer.usage))
    return stats


def cmd_import(args) -> int:
    fiches, warnings = import_fiches(
        args.export, args.client, args.body_col, id_col=args.id_col, title_col=args.title_col,
        sheet=args.sheet, delimiter=args.delimiter, encoding=args.encoding,
    )
    if not fiches:
        raise InputError("no fiche imported: check --id-col and --body-col")
    save_fiches(args.out, fiches)
    say(f"Imported {len(fiches)} fiches for {args.client} into {args.out}.")
    for warning in warnings[:10]:
        say(f"warning: {warning}")
    if len(warnings) > 10:
        say(f"... and {len(warnings) - 10} more warnings")
    return 0


def _load(args):
    fiches, warnings = load_fiches(args.source, args.client)
    if args.limit:
        fiches = fiches[: args.limit]
    if not fiches:
        raise InputError(f"no fiche found in {args.source}")
    say(f"{len(fiches)} fiches loaded from {args.source}.")
    for warning in warnings[:10]:
        say(f"warning: {warning}")
    return fiches, warnings


def _out_dir(args) -> Path:
    return Path(args.out_dir) if args.out_dir else DEFAULT_OUT / args.client


def cmd_profile(args) -> int:
    fiches, _ = _load(args)
    llm = make_llm(args)
    profile = build_profile(args.client, fiches, llm=llm)
    out = Path(args.out) if args.out else _out_dir(args) / "profile.json"
    profile.save(out)
    mapped = sum(1 for h in profile.headings.values() if h.get("role"))
    say(
        f"Profile of {args.client}: {len(profile.headings)} headings ({mapped} with a role), "
        f"{len(profile.boilerplate)} boilerplate lines, stable: {'yes' if profile.stable else 'no'}. Saved to {out}."
    )
    return 0


def cmd_decompose(args) -> int:
    fiches, warnings = _load(args)
    llm = make_llm(args)
    out_dir = _out_dir(args)
    if args.profile:
        profile = Profile.load(args.profile)
    else:
        profile = build_profile(args.client, fiches, llm=llm)
        profile.save(out_dir / "profile.json")
    decomposer = Decomposer(profile=profile, llm=llm)
    results: list[DecomposedFiche] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "fiches.decomposed.jsonl"
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for index, fiche in enumerate(fiches):
            decomposed = decomposer.decompose(fiche)
            results.append(decomposed)
            handle.write(json.dumps(decomposed.to_dict(), ensure_ascii=False) + "\n")
            if every_tenth(index, len(fiches)):
                say(f"  {index + 1}/{len(fiches)}")
    markdown, summary = build_report(args.client, results, profile, llm_stats(llm, decomposer), warnings)
    (out_dir / "report.md").write_text(markdown, encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    say(
        f"{summary['guided']} guided, {summary['citable']} citable, {summary['info_only']} info only; "
        f"{summary['steps_verified']} of {summary['steps']} steps on a verified extract."
    )
    say(f"Written to {out_dir}: fiches.decomposed.jsonl, report.md, summary.json, profile.json")
    return 0


def cmd_show(args) -> int:
    with Path(args.decomposed).open(encoding="utf-8") as handle:
        for line in handle:
            data = json.loads(line)
            if data.get("fiche_id") != args.fiche_id:
                continue
            fiche = DecomposedFiche.from_dict(data)
            print(f"{fiche.fiche_id} · {fiche.title}")
            print(f"status {fiche.status} · confidence {fiche.confidence} · agreement {fiche.methods.get('agreement')}")
            for reason in fiche.reasons:
                print(f"  - {reason}")
            for step in fiche.steps:
                flags = [step.kind, step.role] + (["if failure of the previous step"] if step.after_failure else [])
                print(f"\n{step.n}. {step.text}")
                print(f"   [{', '.join(flags)}] from {'+'.join(step.sources)}")
                if step.condition:
                    print(f"   condition: {step.condition}")
                if step.on_failure:
                    print(f"   on failure: go to step {step.on_failure}")
                if step.goto:
                    print(f"   goes to step {step.goto}")
                if step.instruction:
                    print(f"   reworded: {step.instruction}")
                if step.entities:
                    print(f"   entities: {', '.join(step.entities)}")
            return 0
    raise InputError(f"fiche {args.fiche_id} not found in {args.decomposed}")


def _add_llm_args(parser) -> None:
    parser.add_argument("--llm-config", help="Azure OpenAI settings (JSON); without it the LLM pass does not run")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replay", action="store_true", help="answers from the record only, never call the model")
    mode.add_argument("--refresh", action="store_true", help="call the model again and overwrite the record")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE), help="where LLM answers are recorded")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m kecore", description="KnowledgeEngine core: decompose KB fiches.")
    parser.add_argument("--version", action="version", version=f"kecore {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("import-fiches", help="fiches from a KB export table (CSV, XLSX, JSON, JSONL)")
    p.add_argument("export")
    p.add_argument("--client", required=True)
    p.add_argument("--id-col", required=True, help="fiche number or reference column")
    p.add_argument("--title-col")
    p.add_argument("--body-col", action="append", required=True,
                   help="text column; repeat it for several (each becomes a section named after its column)")
    p.add_argument("--sheet")
    p.add_argument("--delimiter")
    p.add_argument("--encoding")
    p.add_argument("--out", required=True, help="fiches file to write (.jsonl)")
    p.set_defaults(func=cmd_import)

    for name, func, text in (
        ("profile", cmd_profile, "learn the client's writing profile"),
        ("decompose", cmd_decompose, "decompose every fiche into verified steps"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("source", help="folder of fiches, or a fiches .jsonl")
        p.add_argument("--client", required=True)
        p.add_argument("--limit", type=int, help="only the first N fiches")
        p.add_argument("--out-dir", help="default: clients-local/kecore/<client>")
        _add_llm_args(p)
        if name == "profile":
            p.add_argument("--out", help="profile file (default: <out-dir>/profile.json)")
        else:
            p.add_argument("--profile", help="profile file to use (default: learned now and saved)")
        p.set_defaults(func=func)

    p = sub.add_parser("show", help="print the steps of one decomposed fiche")
    p.add_argument("decomposed")
    p.add_argument("fiche_id")
    p.set_defaults(func=cmd_show)
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
    except (InputError, ValueError, FileNotFoundError, AzureError, LLMError) as exc:
        say(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        say("interrupted")
        return 130
