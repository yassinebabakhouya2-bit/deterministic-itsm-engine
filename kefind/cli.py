"""Ligne de commande. Lancer depuis la racine du dépôt :

    python -m kefind decide --ticket "..." --client CLIENT --fiches FICHES.jsonl
        [--understand-llm-config CONFIG] [--write-llm-config CONFIG] [--replay | --refresh]
    python -m kefind calibrate --tickets TICKETS.jsonl --client CLIENT --fiches FICHES.jsonl
        [--max-wrong 0.05]

FICHES.jsonl est le format de kecore (``DecomposedFiche.to_dict()``, une
fiche par ligne) ; TICKETS.jsonl est le format de scoreboard (voir
``scoreboard.dataset``). ``kefind/examples/`` fournit un petit jeu de chacun.
"""

from __future__ import annotations

import argparse
import sys

from kecore.errors import InputError
from kecore.llm import AzureOpenAIChat, LLMError, RecordingLLM, load_llm_config

from . import __version__
from .calibration import calibrate_engine
from .decide import Thresholds
from .engine import KefindEngine
from .io import load_decomposed_jsonl


def say(message: str = "") -> None:
    print(message, file=sys.stderr)


def _make_llm(args, config_path: str | None, cache_dir: str) -> RecordingLLM | None:
    if not config_path:
        return None
    config = load_llm_config(config_path)
    inner = AzureOpenAIChat.from_config(config)
    mode = "replay" if args.replay else "refresh" if args.refresh else "record"
    return RecordingLLM(inner, cache_dir, mode=mode)


def _build_engine(args) -> KefindEngine:
    fiches = [f for f in load_decomposed_jsonl(args.fiches) if f.client == args.client]
    if not fiches:
        raise InputError(f"aucune fiche du client {args.client!r} dans {args.fiches}")
    return KefindEngine(
        fiches_by_client={args.client: fiches},
        thresholds=Thresholds(min_score=args.min_score, gap=args.gap),
        understand_llm=_make_llm(args, args.understand_llm_config, args.understand_cache_dir),
        write_llm=_make_llm(args, args.write_llm_config, args.write_cache_dir),
        name="kefind-cli",
    )


class _CliTicket:
    def __init__(self, client: str, text: str):
        self.ticket_id = "cli"
        self.client = client
        self.text = text

    @property
    def key(self) -> str:
        return f"{self.client}/{self.ticket_id}"


def cmd_decide(args) -> int:
    engine = _build_engine(args)
    decision = engine.decide(_CliTicket(args.client, args.ticket))
    score = f"  (score {decision.score:.3f})" if decision.score is not None else ""
    print(f"décision : {decision.kind}{score}")
    if decision.kind == "question":
        print(f"question : {decision.question}")
    if decision.fiches:
        shown = ", ".join(f"{f} ({decision.titles[f]})" if f in decision.titles else f for f in decision.fiches[:5])
        print(f"candidats : {shown}")
    for item in decision.trace:
        if item.get("step") == "write":
            verified = "oui" if item.get("citation_verified") else "non — repli sur l'étape de la fiche"
            print(f"étape {item.get('step_number')} de {item.get('fiche')} montrée (citation vérifiée : {verified})")
    if decision.error:
        say(f"erreur : {decision.error}")
        return 1
    return 0


def cmd_calibrate(args) -> int:
    from scoreboard.dataset import load_tickets

    engine = _build_engine(args)
    tickets = [t for t in load_tickets(args.tickets) if t.client == args.client and t.labeled]
    calibration = calibrate_engine(engine, tickets, max_wrong=args.max_wrong)
    if calibration is None:
        say("pas assez de décisions avec un score pour calibrer")
        return 1
    for row in calibration.rows:
        marker = ""
        if calibration.recommended is not None and row.threshold == calibration.recommended.threshold:
            marker = "  <-- recommandé"
        print(
            f"seuil {row.threshold} : exact {row.exact.value}, fiche fausse {row.wrong_shown.value} "
            f"(IC95 jusqu'à {row.wrong_shown.high}){marker}"
        )
    return 0


def _add_llm_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--understand-llm-config", help="config Azure OpenAI pour le temps 1 ; sans elle, repli par règles")
    p.add_argument("--write-llm-config", help="config Azure OpenAI pour le temps 5 ; sans elle, repli sur la fiche")
    p.add_argument("--understand-cache-dir", default="clients-local/kefind/llm-cache/understand")
    p.add_argument("--write-cache-dir", default="clients-local/kefind/llm-cache/write")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--replay", action="store_true", help="réponses enregistrées seulement, jamais d'appel")
    mode.add_argument("--refresh", action="store_true", help="appelle le modèle et réécrit l'enregistrement")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m kefind", description="kefind : choisir la bonne fiche (tranche 3).")
    parser.add_argument("--version", action="version", version=f"kefind {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("decide", help="décider pour un ticket donné en ligne de commande")
    p.add_argument("--ticket", required=True)
    p.add_argument("--client", required=True)
    p.add_argument("--fiches", required=True, help="fiches décomposées (.jsonl, format kecore)")
    p.add_argument("--min-score", type=float, default=Thresholds.min_score)
    p.add_argument("--gap", type=float, default=Thresholds.gap)
    _add_llm_args(p)
    p.set_defaults(func=cmd_decide)

    p = sub.add_parser("calibrate", help="balayer le seuil d'abstention sur des tickets étiquetés")
    p.add_argument("--tickets", required=True)
    p.add_argument("--client", required=True)
    p.add_argument("--fiches", required=True)
    p.add_argument("--max-wrong", type=float, default=0.05)
    p.add_argument("--min-score", type=float, default=Thresholds.min_score)
    p.add_argument("--gap", type=float, default=Thresholds.gap)
    _add_llm_args(p)
    p.set_defaults(func=cmd_calibrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args) or 0
    except (InputError, ValueError, FileNotFoundError, LLMError) as exc:
        say(f"erreur : {exc}")
        return 1
    except KeyboardInterrupt:
        say("interrompu")
        return 130
