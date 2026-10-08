"""What the Diagnostic writes back into a ServiceNow ticket (V10 slice 6), as plain code.

The note is built from the session's fiche and its steps only -- never from what the user typed
(a description, a screenshot's text can hold names, numbers, anything), and never from a model's
wording: a step the assistant reformulated (``verbatim_from_kb`` false: a model drafted it, and a
model sees what was typed) is replaced by a pointer to the fiche. It is written to the ticket by a
separate executor (itsm/writeback, a Logic App with its own identity) and only after an ITSM agent
validated it in the web app; the web app never writes to ServiceNow.

Only incidents (INC numbers): the executor reads and writes the incident table.

A fiche whose resolution is an action of the ITSM module's closed list (password reset, MFA reset,
group membership...) can be handed over instead: same note, and the ticket goes to the module's
group, where the ITSM action engine proposes the action and an agent validates it. Which fiches
those are is a rule on the fiche's title, in config (``handover`` rules), never a model's guess.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable, List, Optional

from .kefind_ports import fiche_id_of, is_kefind

TICKET_RE = re.compile(r"INC\d{7,10}")  # always fullmatch
KINDS = ("work_note", "handover")
MAX_NOTE_CHARS = 3800
MAX_STEPS_IN_NOTE = 25
ITSM_ACTIONS = frozenset({"mfa_reset", "password_reset", "group_add", "license_assign", "offboarding"})
REFORMULATED = "(étape reformulée par l'assistant : voir la fiche)"


def outcome(view: dict) -> str:
    guide = view.get("guide") or {}
    total = len(guide.get("steps") or [])
    done = min(int(view.get("current_step") or 0), total)
    state = view.get("state")
    if state == "SOLVED":
        return f"Résolu : confirmé par l'utilisateur après les {total} étapes de la fiche."
    if total and done >= total:
        return f"Toutes les étapes faites ({total}/{total}), résolution non confirmée."
    return f"En cours : {done} étape(s) faite(s) sur {total}."


def fiche_reference(guide: dict) -> str:
    parent = str(guide.get("parent_id") or "")
    return fiche_id_of(parent) if is_kefind(parent) else parent


def work_note(view: dict, validated_by: str, when: datetime) -> str:
    """The internal work note for a session that has a fiche; '' when it has none."""
    guide = view.get("guide") or {}
    steps = guide.get("steps") or []
    if not guide or not steps:
        return ""
    reference = fiche_reference(guide)
    lines = [f"[KnowledgeEngine] Diagnostic guidé — fiche « {guide.get('title') or reference} »",
             f"Référence de la fiche : {reference}" + (" (fiche la plus proche, non confirmée)" if guide.get("approximate") else ""),
             f"Résultat : {outcome(view)}", "Étapes de la fiche :"]
    for step in steps[:MAX_STEPS_IN_NOTE]:
        text = " ".join(str(step.get("instruction") or "").split()) if step.get("verbatim_from_kb") else REFORMULATED
        lines.append(f"{step.get('order')}. {text or REFORMULATED}")
    lines.append(f"Validé par {validated_by} le {when.strftime('%d/%m/%Y %H:%M')} UTC.")
    note = "\n".join(lines)
    return note if len(note) <= MAX_NOTE_CHARS else note[: MAX_NOTE_CHARS - 1] + "…"


def handover_action(title: str, rules: Iterable[dict]) -> Optional[str]:
    """The ITSM action a fiche's title calls for, from the configured rules (first match), else None."""
    for rule in rules or []:
        action, pattern = rule.get("action"), rule.get("label_pattern")
        if action in ITSM_ACTIONS and pattern and re.search(pattern, title or ""):
            return action
    return None


def request_row(view: dict, kind: str, note: str, by_id: str, by_name: str, when: datetime,
                action: Optional[str] = None) -> dict:
    """The diagwriteback row the executor reads: status 'validated' (an agent asked for it) and an
    empty executionStatus (not executed yet). validatedAtUtc is 'YYYY-MM-DDTHH:MM:SSZ', compared as
    text by the executor."""
    if kind not in KINDS:
        raise ValueError("unknown kind")
    ticket = str(view.get("ticket_id") or "")
    if not TICKET_RE.fullmatch(ticket):
        raise ValueError("the session has no ServiceNow incident number")
    if not note:
        raise ValueError("nothing to write: the session has no fiche")
    if kind == "handover":
        note += f"\nTransféré au module ITSM : action proposée « {action} »." if action else ""
    return {"PartitionKey": view["client_id"], "RowKey": f"{view['session_id']}-{kind}", "ticketNumber": ticket,
            "sessionId": view["session_id"], "kind": kind, "noteText": note[:MAX_NOTE_CHARS + 120],
            "handoverAction": action or "", "status": "validated", "executionStatus": "",
            "validatedById": by_id, "validatedByName": by_name, "validatedAtUtc": when.strftime("%Y-%m-%dT%H:%M:%SZ")}


STATUS_LABELS = {"running": "en cours d'écriture", "success": "écrit dans le ticket",
                 "dry_run": "simulé (aucune écriture)", "not_found": "ticket introuvable dans ServiceNow",
                 "inactive": "ticket clos dans ServiceNow : rien écrit", "error": "erreur d'écriture"}


def statuses(rows: List[dict]) -> dict:
    """kind -> what happened to the request, for the session page."""
    return {row.get("kind"): STATUS_LABELS.get(row.get("executionStatus") or "", "validé, en attente de l'exécuteur")
            for row in rows}


__all__ = ["work_note", "outcome", "fiche_reference", "handover_action", "request_row", "statuses", "TICKET_RE",
           "KINDS", "STATUS_LABELS"]
