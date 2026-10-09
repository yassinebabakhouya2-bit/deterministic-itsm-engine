# =====================================================================
# KnowledgeEngine -- labeling tab (V10 slice 4)
#
# A technician tells, ticket by ticket, which fiche of the client's KB the ticket needed
# (or that no fiche covers it). These labels are the ground truth of the scoreboard run by
# fn-kecore (POST /api/kecore/scoreboard/runs): exact fiche @1, wrong fiche shown, and the
# calibrated floor of the funnel. The tab only WRITES labels (table ticketlabels); tickets,
# the engine's findings and the fiche catalog are written by fn-kecore and only read here.
#
# Against a biased ground truth:
#   - the engine's proposal is shown next to the ticket, never pre-checked: confirming it is
#     one deliberate click, any other fiche can be picked from the whole catalog;
#   - the next ticket is drawn from the engine's outcome (fiche shown / question / abstain)
#     whose labels are fewest, so the hard tickets get labeled too, not only the easy ones;
#     each labeler gets their own shuffled order, so two labelers rarely meet on a ticket, and
#     a label saved meanwhile by someone else is never overwritten silently;
#   - "I don't know" skips a ticket without inventing a label.
#
# Reference questions (2026-10-09, table refquestions): a question typed the way a technician asks
# it ("comment attribuer une ligne teams") with the fiche it needs, or none. Real tickets cannot hold
# those short typed questions; the scoreboard measures both, and lists every reference question in a
# non-regression section of its report. The question is the row's identity (ref-<sha256 of its
# normalized text>): typing it twice edits the same row, never a duplicate; its fiche can be changed,
# its text cannot (a new wording is a new question). config/reference-questions.yaml holds the
# questions the repository ships with; a person imports them with one click (never on a page view),
# create-only: a question already there keeps the label a person gave it.
#
# Security rules enforced SERVER-SIDE (never trusted from the form):
#   - access: tenant + Entra group (config/labels.yaml, else config/itsm.yaml's agents) from
#     Easy Auth's validated claims only, AND the client must be one the user may see
#     (app/auth.py) -- deny-by-default;
#   - every fiche id posted must exist in the client's catalog, every ticket in its table;
#   - CSRF: POSTs must carry an Origin/Referer of this same host.
# Ticket text is untrusted (written by end users, scrubbed by fn-kecore): templates are
# autoescaped and nothing is marked |safe.
# =====================================================================
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml
from flask import Blueprint, abort, redirect, render_template_string, request, url_for

from auth import has_tenant_and_group, parse_client_principal, resolve_display_name, resolve_user_id

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
with open(_CONFIG_DIR / "labels.yaml", encoding="utf-8") as _f:
    LABELS_CONFIG = yaml.safe_load(_f) or {}
with open(_CONFIG_DIR / "itsm.yaml", encoding="utf-8") as _f:
    _ITSM_ACCESS = (yaml.safe_load(_f) or {}).get("access") or {}

_ACCESS = LABELS_CONFIG.get("access") or _ITSM_ACCESS
_TABLES = {"tickets": "tickets", "fiches": "kefindfiches", "labels": "ticketlabels", "scores": "kecorescores",
           "refs": "refquestions", **(LABELS_CONFIG.get("tables") or {})}
_REF_SEEDS_PATH = _CONFIG_DIR / "reference-questions.yaml"
REF_PREFIX = "ref-"          # the scoreboard reads only rows with this prefix (kecore_func/scoreboard_service.py)
REF_MIN_CHARS, REF_MAX_CHARS = 3, 500
_LOCAL_DEV = os.environ.get("LOCAL_DEV_LABELS") == "1"
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # always fullmatch: "$" would let a trailing newline in

# The fields shown to the labeler, in this order (slugs written by kecore.tickets.slug).
TICKET_FIELDS = [
    ("titre", "Titre"), ("sujet", "Sujet"), ("application_service", "Application / service"),
    ("description", "Description"), ("resolution", "Résolution"), ("cause_reelle", "Cause réelle"),
    ("sujet_complet", "Catégorie"),
]
KIND_LABELS = {"fiche": "Fiche montrée", "question": "Question posée", "abstain": "Abstention",
               "error": "Erreur", "unseen": "Pas encore passé dans le moteur"}
DECISIONS = ("confirm", "select", "none", "skip")
# Messages are passed between pages as codes, never as free text (no text injected by a crafted link).
MESSAGES = {
    "saved": "Enregistré.",
    "skipped": "Passé.",
    "done": "Tous les tickets sont étiquetés ou passés.",
    "no_shown": "Le moteur n'a montré aucune fiche connue pour ce ticket : choisissez-la dans la liste.",
    "unknown_fiche": "Fiche inconnue : choisissez-la dans la liste proposée.",
    "empty": "Cochez au moins une fiche, ou choisissez « Aucune fiche ne couvre ce ticket ».",
    "changed": "Ce ticket vient d'être étiqueté par quelqu'un d'autre : vérifiez son étiquette puis recommencez.",
    "ref_saved": "Question de référence enregistrée.",
    "ref_deleted": "Question de référence supprimée.",
    "ref_text": "La question doit faire entre 3 et 500 caractères.",
    "ref_exists": "Cette question existe déjà : modifiez sa fiche ci-dessous.",
    "ref_changed": "Cette question vient d'être modifiée par quelqu'un d'autre : vérifiez-la puis recommencez.",
    "ref_seeded": "Questions du dépôt importées. Celles déjà présentes gardent leur étiquette.",
    "ref_seeded_partial": "Questions du dépôt importées, sauf celles marquées « non importable » ci-dessous : leur fiche "
                          "est absente de la carte actuelle ou ambiguë. Celles déjà présentes gardent leur étiquette.",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _claims():
    header = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    return None if header is None else parse_client_principal(header)


def labels_access_for_request() -> bool:
    """Tenant + group gate of the tab (the per-client check comes on top, in each route)."""
    claims = _claims()
    if claims is None:
        return _LOCAL_DEV
    return has_tenant_and_group(claims, _ACCESS.get("entraTenantId"), _ACCESS.get("entraGroup"))


def _same_origin() -> bool:
    src = request.headers.get("Origin") or request.headers.get("Referer") or ""
    return bool(src) and urlparse(src).netloc == request.host


def _order(user_id: str, row_key: str) -> str:
    """A stable pseudo-random order of its own for each labeler (the export's order would cluster
    similar tickets, and one shared order would send two labelers to the same ticket)."""
    return hashlib.sha256(f"{user_id}\x00{row_key}".encode("utf-8")).hexdigest()


# A fiche named by its number alone ("KB0233", "kb 233", "K0090"): on client-s a fiche id is the document's
# name ("KB0233 -  Associate a phone line": kecore keeps the whole stem when the number has fewer than 5
# digits), so the repository's seeds and a technician typing a number are resolved against the catalog.
_NUMBER_ONLY_RE = re.compile(r"(?i)^\s*KB?\s*0*(\d{1,7})\s*$")
_LEADING_NUMBER_RE = re.compile(r"(?i)^\s*KB?\s*0*(\d{1,7})(?!\d)")


def resolve_fiche(token: str, catalog: dict) -> tuple[str | None, str]:
    """(fiche id, "") when ``token`` is a catalog id, or a number exactly one catalog fiche starts with;
    else (None, why): "absente de la carte" or "ambiguë : <ids>". Never a guess between two fiches."""
    token = (token or "").strip()
    if token in catalog:
        return token, ""
    match = _NUMBER_ONLY_RE.match(token)
    if not match:
        return None, "absente de la carte"
    number = int(match.group(1))
    hits = sorted(f for f in catalog if (m := _LEADING_NUMBER_RE.match(f)) and int(m.group(1)) == number)
    if len(hits) == 1:
        return hits[0], ""
    return None, ("ambiguë : " + " | ".join(hits)) if hits else "absente de la carte"


def ref_id(text: str) -> str:
    """The row id of a reference question: its normalized text (case, spacing), hashed."""
    normalized = " ".join(text.split()).casefold()
    return REF_PREFIX + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def load_ref_seeds(path: Path = _REF_SEEDS_PATH) -> dict:
    """client -> [(question, [fiche ids])] from config/reference-questions.yaml; a malformed entry is skipped."""
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    out: dict = {}
    for client, items in (data.items() if isinstance(data, dict) else []):
        for item in items if isinstance(items, list) else []:
            text = " ".join(str((item or {}).get("text") or "").split()) if isinstance(item, dict) else ""
            expected = item.get("expected") if isinstance(item, dict) else None
            if REF_MIN_CHARS <= len(text) <= REF_MAX_CHARS and isinstance(expected, list) \
                    and all(isinstance(e, str) and e for e in expected):
                out.setdefault(str(client), []).append((text, list(expected)))
    return out


def _candidates(ticket: dict) -> list:
    try:
        value = json.loads(ticket.get("kefind_candidates") or "[]")
    except ValueError:
        return []
    return [v for v in value if isinstance(v, str)]


class Conflict(Exception):
    """Someone wrote the row between this request's read and its write."""


class AzureTable:
    """The same small contract as fn-kecore's TableStorage, over an azure.data.tables TableClient.
    ``get`` returns the row with its ETag under "_etag" (never written back)."""

    def __init__(self, table_client):
        self._client = table_client

    def list(self, client, select=None):
        rows = self._client.query_entities("PartitionKey eq @pk", parameters={"pk": client}, select=select)
        return sorted((dict(r) for r in rows), key=lambda r: r["RowKey"])

    def get(self, client, row_key):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self._client.get_entity(partition_key=client, row_key=row_key)
        except ResourceNotFoundError:
            return None
        return {**dict(entity), "_etag": entity.metadata.get("etag")}

    def upsert(self, entity):
        from azure.data.tables import UpdateMode

        self._client.upsert_entity(entity, mode=UpdateMode.MERGE)

    def create(self, entity):
        """Inserts, or raises Conflict when the row already exists (two first labels at once)."""
        from azure.core.exceptions import ResourceExistsError

        try:
            self._client.create_entity(entity)
        except ResourceExistsError:
            raise Conflict() from None

    def delete(self, client, row_key):
        self._client.delete_entity(partition_key=client, row_key=row_key)

    def update(self, entity, etag):
        """Merges into the row only if it is still the one read (If-Match), else raises Conflict."""
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
        from azure.data.tables import UpdateMode

        try:
            self._client.update_entity(entity, mode=UpdateMode.MERGE, etag=etag,
                                       match_condition=MatchConditions.IfNotModified)
        except (ResourceModifiedError, ResourceNotFoundError):
            raise Conflict() from None


def azure_tables(table_service) -> dict:
    """The five tables, labels and refs created if missing (the other three belong to fn-kecore)."""
    for key in ("labels", "refs"):
        try:
            table_service.create_table(_TABLES[key])
        except Exception:
            pass  # exists, or the role is not propagated yet: the first write will say so
    return {key: AzureTable(table_service.get_table_client(name)) for key, name in _TABLES.items()}


def create_labels_blueprint(tables: dict, deps: dict):
    """tables: tickets / fiches / labels / scores / refs, each with list(client, select), get(client, key),
    upsert(entity); labels and refs also with create(entity) and update(entity, etag), both raising
    Conflict, and refs with delete(client, key). deps: allowed_clients(), user_id(), display_name(),
    label_access(); ref_seeds (optional): client -> [(question, [fiche ids])], else the repository's file."""
    bp = Blueprint("labels", __name__)
    ref_seeds = deps.get("ref_seeds")

    @bp.app_context_processor
    def _nav():
        try:
            return {"labels_nav": bool(deps["label_access"]() and deps["allowed_clients"]())}
        except Exception:
            return {"labels_nav": False}

    def _guard(client=None) -> list:
        if not deps["label_access"]():
            abort(403)
        clients = list(deps["allowed_clients"]())
        if not clients or (client is not None and client not in clients):
            abort(403)
        return clients

    def _catalog(client) -> dict:
        return {r["fiche_id"]: r for r in tables["fiches"].list(client) if r.get("fiche_id")}

    def _progress(client) -> dict:
        tickets = tables["tickets"].list(client, select=["RowKey", "kefind_kind"])
        labels = {r["RowKey"]: r for r in tables["labels"].list(client)}
        by_kind = defaultdict(lambda: {"total": 0, "labeled": 0})
        remaining: dict = defaultdict(list)
        counts = Counter()
        for t in tickets:
            kind = t.get("kefind_kind") or "unseen"
            by_kind[kind]["total"] += 1
            label = labels.get(t["RowKey"])
            if label is None:
                remaining[kind].append(t["RowKey"])
                counts["remaining"] += 1
                continue
            by_kind[kind]["labeled"] += 1
            if label.get("skipped"):
                counts["skipped"] += 1
            elif label.get("expected") == "[]":
                counts["none"] += 1
            else:
                counts["with_fiche"] += 1
        counts["tickets"] = len(tickets)
        counts["labeled"] = counts["with_fiche"] + counts["none"]
        return {"counts": counts, "by_kind": dict(by_kind), "remaining": remaining, "labels": labels}

    def _next(progress) -> str | None:
        """The next ticket: from the engine outcome whose labels are fewest, in this labeler's order."""
        pending = {k: v for k, v in progress["remaining"].items() if v}
        if not pending:
            return None
        user = deps["user_id"]() or ""
        kind = min(pending, key=lambda k: (progress["by_kind"][k]["labeled"], k))
        return min(pending[kind], key=lambda row_key: _order(user, row_key))

    @bp.route("/labels")
    def home():
        clients = _guard()
        client = request.args.get("client") or clients[0]
        if client not in clients:
            abort(403)
        error = None
        progress = {"counts": Counter(), "by_kind": {}, "labels": {}}
        score = None
        try:
            progress = _progress(client)
            scores = tables["scores"].list(client)
            score = scores[-1] if scores else None
        except Exception as exc:  # role not propagated, table missing...
            error = f"Lecture impossible : {type(exc).__name__}"
        recent = sorted(progress["labels"].values(), key=lambda r: r.get("labeled_at", ""), reverse=True)[:15]
        refs = []
        try:
            refs = _refs(client)
        except Exception as exc:
            error = error or f"Lecture impossible : {type(exc).__name__}"
        return render_template_string(
            PAGE, view="home", clients=clients, client=client, p=progress, score=score, recent=recent, refs=refs,
            kinds=KIND_LABELS, error=error, msg=MESSAGES.get(request.args.get("msg", "")),
            display_name=deps["display_name"]())

    @bp.route("/labels/<client>/next")
    def next_ticket(client):
        _guard(client)
        ticket_id = _next(_progress(client))
        if ticket_id is None:
            return redirect(url_for("labels.home", client=client, msg="done"))
        return redirect(url_for("labels.ticket", client=client, ticket_id=ticket_id, msg=request.args.get("msg")))

    @bp.route("/labels/<client>/t/<ticket_id>")
    def ticket(client, ticket_id):
        _guard(client)
        if not _ID_RE.fullmatch(ticket_id):
            abort(404)
        row = tables["tickets"].get(client, ticket_id)
        if row is None:
            abort(404)
        catalog = _catalog(client)
        candidates = [c for c in _candidates(row) if c in catalog]
        label = tables["labels"].get(client, ticket_id)
        current = json.loads(label["expected"]) if label and label.get("expected") else None
        fields = [(name, row.get(key)) for key, name in TICKET_FIELDS if row.get(key)]
        options = sorted(catalog.values(), key=lambda r: (r.get("label") or r["fiche_id"]).lower())
        return render_template_string(
            PAGE, view="ticket", client=client, ticket_id=ticket_id, row=row, fields=fields, catalog=catalog,
            candidates=candidates, shown=row.get("kefind_fiche") or None, kinds=KIND_LABELS, label=label,
            current=current, options=options, msg=MESSAGES.get(request.args.get("msg", "")),
            seen=(label or {}).get("labeled_at", ""), display_name=deps["display_name"]())

    @bp.route("/labels/<client>/t/<ticket_id>", methods=["POST"])
    def save(client, ticket_id):
        _guard(client)
        if not _same_origin():
            abort(403)
        if not _ID_RE.fullmatch(ticket_id):
            abort(404)
        row = tables["tickets"].get(client, ticket_id)
        if row is None:
            abort(404)
        decision = request.form.get("decision", "")
        if decision not in DECISIONS:
            abort(400)
        catalog = _catalog(client)

        def back(code):
            return redirect(url_for("labels.ticket", client=client, ticket_id=ticket_id, msg=code))

        existing = tables["labels"].get(client, ticket_id)
        if (existing or {}).get("labeled_at", "") != request.form.get("seen", ""):
            return back("changed")  # someone labeled it since this page was shown: never overwrite blindly

        expected: list = []
        if decision == "confirm":
            shown = row.get("kefind_fiche") or ""
            if shown not in catalog:
                return back("no_shown")
            expected = [shown]
        elif decision == "select":
            picked = request.form.getlist("fiche") + [request.form.get("other", "").strip()]
            for fiche_id in picked:
                if fiche_id and fiche_id not in expected:
                    if fiche_id not in catalog:
                        return back("unknown_fiche")
                    expected.append(fiche_id)
            if not expected:
                return back("empty")
        user_id = deps["user_id"]() or "unknown-user"
        entity = {
            "PartitionKey": client, "RowKey": ticket_id,
            "expected": json.dumps(expected, ensure_ascii=False) if decision != "skip" else "",
            "skipped": decision == "skip", "decision": decision,
            "labeled_by_id": user_id, "labeled_by_name": deps["display_name"]() or "Utilisateur",
            "labeled_at": _now_utc(),
            "proposal_kind": row.get("kefind_kind") or "", "proposal_fiche": row.get("kefind_fiche") or "",
            "proposal_kb_run": row.get("kefind_kb_run") or "",
        }
        try:  # atomic: a first label is an insert, a correction a conditional update of the row read
            if existing is None:
                tables["labels"].create(entity)
            else:
                tables["labels"].update(entity, existing.get("_etag"))
        except Conflict:
            return back("changed")
        return redirect(url_for("labels.next_ticket", client=client, msg="skipped" if decision == "skip" else "saved"))

    # ------------------------------------------------------------ reference questions
    def _refs(client) -> list:
        rows = [r for r in tables["refs"].list(client) if str(r.get("RowKey", "")).startswith(REF_PREFIX)]
        for r in rows:
            try:
                r["expected_list"] = json.loads(r.get("expected") or "[]")
            except ValueError:
                r["expected_list"] = []
        return sorted(rows, key=lambda r: (r.get("text") or "").casefold())

    def _seeds(client) -> list:
        return (ref_seeds if ref_seeds is not None else load_ref_seeds()).get(client, [])

    def _resolve_all(tokens, catalog) -> tuple[list, str]:
        """Every expected fiche resolved, or ([], the first reason one is not)."""
        out = []
        for token in tokens:
            fiche_id, why = resolve_fiche(token, catalog)
            if fiche_id is None:
                return [], f"{token} : {why}"
            if fiche_id not in out:
                out.append(fiche_id)
        return out, ""

    def _seed_status(client, catalog, stored) -> list:
        """What the import would do with each repository question, computed on the page, written nowhere. A
        question already stored shows the fiche stored (a person's label wins), flagged when it is no longer
        in the map or differs from the repository's."""
        status = []
        for text, expected in _seeds(client):
            resolved, why = _resolve_all(expected, catalog)
            row = stored.get(ref_id(text))
            if row is None:
                status.append({"text": text, "expected": expected, "fiches": resolved, "why": why,
                               "state": "à importer" if not why else "non importable"})
                continue
            kept = row.get("expected_list") or []
            if any(f not in catalog for f in kept):
                state = "présente — fiche absente de la carte"
            elif not why and sorted(kept) != sorted(resolved):
                state = "présente — fiche différente du dépôt"
            else:
                state = "présente"
            status.append({"text": text, "expected": expected, "fiches": kept, "why": "", "state": state})
        return status

    def _ref_entity(client, rid, text, expected, source) -> dict:
        return {"PartitionKey": client, "RowKey": rid, "text": text,
                "expected": json.dumps(expected, ensure_ascii=False), "source": source,
                "labeled_by_id": deps["user_id"]() or "unknown-user",
                "labeled_by_name": deps["display_name"]() or "Utilisateur", "labeled_at": _now_utc()}

    @bp.route("/labels/<client>/refs")
    def refs(client):
        _guard(client)
        catalog = _catalog(client)
        editing = None
        rid = request.args.get("id", "")
        if rid:
            if not _ID_RE.fullmatch(rid) or not rid.startswith(REF_PREFIX):
                abort(404)
            editing = tables["refs"].get(client, rid)
            if editing is None:
                abort(404)
            editing["expected_list"] = json.loads(editing.get("expected") or "[]")
        options = sorted(catalog.values(), key=lambda r: (r.get("label") or r["fiche_id"]).lower())
        rows = _refs(client)
        return render_template_string(
            PAGE, view="refs", client=client, refs=rows, catalog=catalog, options=options, editing=editing,
            seeds=_seed_status(client, catalog, {r["RowKey"]: r for r in rows}),
            msg=MESSAGES.get(request.args.get("msg", "")), display_name=deps["display_name"]())

    @bp.route("/labels/<client>/refs", methods=["POST"])
    def save_ref(client):
        _guard(client)
        if not _same_origin():
            abort(403)
        catalog = _catalog(client)
        rid = request.form.get("id", "")

        def back(code, row_id=""):
            return redirect(url_for("labels.refs", client=client, msg=code, **({"id": row_id} if row_id else {})))

        decision = request.form.get("decision", "")
        if decision not in ("fiche", "none"):
            abort(400)
        if rid:                                         # an existing question: only its fiche changes
            if not _ID_RE.fullmatch(rid) or not rid.startswith(REF_PREFIX):
                abort(404)
            existing = tables["refs"].get(client, rid)
            if existing is None:
                abort(404)
            if existing.get("labeled_at", "") != request.form.get("seen", ""):
                return back("ref_changed", rid)
            text = existing.get("text") or ""
        else:
            existing = None
            text = " ".join((request.form.get("text") or "").split())
            if not REF_MIN_CHARS <= len(text) <= REF_MAX_CHARS:
                return back("ref_text")
            rid = ref_id(text)
        expected: list = []
        if decision == "fiche":
            fiche_id, _ = resolve_fiche(request.form.get("fiche") or "", catalog)
            if fiche_id is None:
                return back("unknown_fiche", rid if existing else "")
            expected = [fiche_id]
        entity = _ref_entity(client, rid, text, expected, (existing or {}).get("source") or "form")
        try:
            if existing is None:
                tables["refs"].create(entity)
            else:
                tables["refs"].update(entity, existing.get("_etag"))
        except Conflict:
            return back("ref_exists" if existing is None else "ref_changed", rid)
        return back("ref_saved")

    @bp.route("/labels/<client>/refs/<rid>/delete", methods=["POST"])
    def delete_ref(client, rid):
        _guard(client)
        if not _same_origin():
            abort(403)
        if not _ID_RE.fullmatch(rid) or not rid.startswith(REF_PREFIX):
            abort(404)
        tables["refs"].delete(client, rid)
        return redirect(url_for("labels.refs", client=client, msg="ref_deleted"))

    @bp.route("/labels/<client>/refs/seed", methods=["POST"])
    def seed_refs(client):
        """The repository's reference questions, create-only: a question already there is left as it is."""
        _guard(client)
        if not _same_origin():
            abort(403)
        catalog = _catalog(client)
        present = {r["RowKey"] for r in _refs(client)}
        skipped = 0
        for text, expected in _seeds(client):
            if ref_id(text) in present:
                continue                # create-only: a stored question keeps its label
            resolved, why = _resolve_all(expected, catalog)
            if why:
                skipped += 1           # listed on the page with its reason, never imported half-right
                continue
            try:
                tables["refs"].create(_ref_entity(client, ref_id(text), text, resolved, "seed"))
            except Conflict:
                continue
        return redirect(url_for("labels.refs", client=client, msg="ref_seeded_partial" if skipped else "ref_seeded"))

    return bp


PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KnowledgeEngine — Labellisation</title>
<style>
  :root{--bg:#f7f4ef;--panel:#fff;--panel-2:#faf3ea;--line:#e8ddd0;--txt:#20211f;--muted:#7a7267;
    --accent:#e2703a;--accent-tint:#fbe9dc;--blue:#5c85cf;--blue-tint:#e9f0fb;--ok:#1f9d76;--ok-tint:#e3f5ee;
    --warn:#8a6a3f;--warn-tint:#fdf0c6;--btn:#14172a}
  *{box-sizing:border-box}
  body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt);line-height:1.55}
  header{padding:14px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;background:var(--panel-2);flex-wrap:wrap}
  header h1{margin:0;font-size:1.02rem;font-weight:650}
  header nav{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
  header nav a{color:var(--txt);text-decoration:none;font-size:.85rem}
  header nav a.on{font-weight:650;border-bottom:2px solid var(--accent)}
  .who{margin-left:auto;color:var(--muted);font-size:.8rem}
  main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
  .card h2{margin:0 0 10px;font-size:.98rem}
  .grid{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}
  @media (max-width:820px){.grid{grid-template-columns:1fr}}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
  .stat{background:var(--panel-2);border-radius:8px;padding:10px 12px}
  .stat b{display:block;font-size:1.3rem}
  .stat span{color:var(--muted);font-size:.78rem}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:.86rem;vertical-align:top}
  th{color:var(--muted);font-size:.76rem;text-transform:uppercase;letter-spacing:.03em}
  .muted{color:var(--muted);font-size:.85rem}
  .msg{padding:10px 12px;border-radius:8px;background:var(--blue-tint);margin-bottom:14px;font-size:.86rem}
  .err{background:#fbe4e0;color:#b3372a}
  pre.f{white-space:pre-wrap;font-family:inherit;font-size:.88rem;background:var(--panel-2);padding:10px;border-radius:8px;margin:4px 0 12px;overflow-wrap:anywhere}
  .k{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.03em}
  .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.75rem;font-weight:600;background:var(--blue-tint);color:var(--blue)}
  .badge.eng{background:var(--accent-tint);color:var(--accent)}
  .cand{display:flex;gap:8px;align-items:flex-start;padding:8px 10px;border:1px solid var(--line);border-radius:8px;margin:6px 0}
  .cand.shown{border-color:var(--accent)}
  .btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
  button,.btn{font:inherit;font-weight:650;border:0;border-radius:8px;padding:9px 14px;cursor:pointer;text-decoration:none;display:inline-block}
  .b-ok{background:var(--ok);color:#fff}.b-main{background:var(--btn);color:#fff}
  .b-soft{background:var(--line);color:var(--txt)}
  input[type=text],select{width:100%;font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;background:#fff}
  .bar{height:8px;background:var(--line);border-radius:99px;overflow:hidden;margin:8px 0 2px}.bar i{display:block;height:100%;background:var(--ok)}
  a{color:var(--txt)}
</style>
</head>
<body>
<header>
  <h1>KnowledgeEngine — Labellisation</h1>
  <nav><a href="/diag">Assistant</a><a href="/itsm">Tickets ITSM</a><a class="on" href="/labels">Labellisation</a><a href="/dictionary">Dictionnaire</a></nav>
  <span class="who">{{ display_name }}</span>
</header>
<main>
{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
{% if error %}<div class="msg err">{{ error }}</div>{% endif %}

{% if view == "home" %}
  {% if clients|length > 1 %}
  <form method="get" action="/labels" class="card"><label class="k">Client
    <select name="client" onchange="this.form.submit()">{% for c in clients %}<option value="{{ c }}" {{ 'selected' if c == client }}>{{ c }}</option>{% endfor %}</select></label></form>
  {% endif %}
  <div class="card">
    <h2>Tickets réels de {{ client }}</h2>
    <div class="stats">
      <div class="stat"><b>{{ p.counts.tickets or 0 }}</b><span>tickets</span></div>
      <div class="stat"><b>{{ p.counts.labeled or 0 }}</b><span>étiquetés ({{ p.counts.with_fiche or 0 }} avec fiche, {{ p.counts.none or 0 }} sans)</span></div>
      <div class="stat"><b>{{ p.counts.skipped or 0 }}</b><span>passés</span></div>
      <div class="stat"><b>{{ p.counts.remaining or 0 }}</b><span>restants</span></div>
    </div>
    {% set n = p.counts.labeled or 0 %}
    <p class="muted" style="margin-top:12px">Premier score : dès quelques dizaines d'étiquettes (marge d'erreur large).
      Seuil calibré : la moitié des étiquettes sert à le choisir, l'autre à le vérifier — prouver « au plus 10 % de fiches fausses »
      demande au moins 70 tickets étiquetés (35 par moitié), « au plus 5 % » au moins 146.</p>
    <div class="bar"><i style="width:{{ [n * 100 // 146, 100]|min }}%"></i></div>
    <p class="muted">{{ n }} / 146</p>
    <div class="btns"><a class="btn b-main" href="/labels/{{ client }}/next">Étiqueter le ticket suivant →</a></div>
  </div>
  <div class="grid">
    <div class="card">
      <h2>Par réponse du moteur</h2>
      <table><tr><th>Réponse</th><th>Tickets</th><th>Étiquetés ou passés</th></tr>
      {% for kind, v in p.by_kind.items() %}<tr><td>{{ kinds.get(kind, kind) }}</td><td>{{ v.total }}</td><td>{{ v.labeled }}</td></tr>
      {% else %}<tr><td colspan="3" class="muted">Aucun ticket : lancez POST /api/kecore/tickets/runs.</td></tr>{% endfor %}</table>
      <p class="muted">Le ticket suivant est tiré de la réponse la moins étiquetée : les cas difficiles comptent autant que les faciles.</p>
    </div>
    <div class="card">
      <h2>Dernier score</h2>
      {% if score %}
        <p>Fiche exacte en premier : <b>{{ score.exact_k }} / {{ score.exact_n }}</b><br>
           Fiche fausse montrée : <b>{{ score.wrong_k }} / {{ score.wrong_n }}</b><br>
           Bonne fiche dans les 5 premières : <b>{{ score.recall_k }} / {{ score.recall_n }}</b></p>
        <p class="muted">Seuil recommandé : {{ score.recommended_min_show or '—' }} ({{ 'confirmé' if score.confirmed else 'non confirmé' }}) — {{ score.reason }}<br>
          Run {{ score.RowKey }} · carte {{ score.kb_run }}</p>
      {% else %}<p class="muted">Pas encore de score : il se calcule avec POST /api/kecore/scoreboard/runs.</p>{% endif %}
    </div>
  </div>
  <div class="card">
    <h2>Questions de référence</h2>
    <p class="muted">Des questions telles qu'un technicien les tape (« comment attribuer une ligne teams »), avec la fiche attendue
      ou aucune : le scoreboard les mesure avec les tickets et les liste une par une (non-régression).</p>
    <p><b>{{ refs|length }}</b> question{{ 's' if refs|length > 1 }}{% if score and score.ref_n %} — dernier score : <b>{{ score.ref_k }} / {{ score.ref_n }}</b> justes{% endif %}</p>
    <div class="btns"><a class="btn b-main" href="/labels/{{ client }}/refs">Gérer les questions de référence →</a></div>
  </div>
  <div class="card">
    <h2>Dernières étiquettes</h2>
    <table><tr><th>Ticket</th><th>Étiquette</th><th>Par</th></tr>
    {% for r in recent %}<tr><td><a href="/labels/{{ client }}/t/{{ r.RowKey }}">{{ r.RowKey }}</a></td>
      <td>{% if r.skipped %}<span class="muted">passé</span>{% elif r.expected == '[]' %}aucune fiche{% else %}{{ r.expected }}{% endif %}</td>
      <td class="muted">{{ r.labeled_by_name }} — {{ (r.labeled_at or '')[:16].replace('T', ' ') }}</td></tr>
    {% else %}<tr><td colspan="3" class="muted">Aucune étiquette pour l'instant.</td></tr>{% endfor %}</table>
  </div>

{% elif view == "refs" %}
  <p><a class="muted" href="/labels?client={{ client }}">← Retour</a></p>
  <div class="grid">
    <div class="card">
      {% if editing %}
      <h2>Modifier la fiche attendue</h2>
      <pre class="f">{{ editing.text }}</pre>
      <form method="post" action="/labels/{{ client }}/refs">
        <input type="hidden" name="id" value="{{ editing.RowKey }}"><input type="hidden" name="seen" value="{{ editing.labeled_at }}">
        <div class="k">Fiche attendue</div>
        <input type="text" name="fiche" list="all-fiches" value="{{ editing.expected_list[0] if editing.expected_list else '' }}" placeholder="Tapez quelques mots du titre…" autocomplete="off">
        <div class="btns"><button class="b-main" name="decision" value="fiche">Enregistrer</button>
          <button class="b-soft" name="decision" value="none">Aucune fiche ne couvre cette question</button>
          <a class="btn b-soft" href="/labels/{{ client }}/refs">Annuler</a></div>
      </form>
      {% else %}
      <h2>Nouvelle question de référence</h2>
      <form method="post" action="/labels/{{ client }}/refs">
        <div class="k">Question, telle qu'un technicien la tape</div>
        <input type="text" name="text" maxlength="500" placeholder="comment attribuer une ligne teams" autocomplete="off">
        <div class="k" style="margin-top:10px">Fiche attendue</div>
        <input type="text" name="fiche" list="all-fiches" placeholder="Tapez quelques mots du titre…" autocomplete="off">
        <div class="btns"><button class="b-main" name="decision" value="fiche">Ajouter avec cette fiche</button>
          <button class="b-soft" name="decision" value="none">Ajouter : aucune fiche ne couvre cette question</button></div>
      </form>
      {% endif %}
      <datalist id="all-fiches">{% for o in options %}<option value="{{ o.fiche_id }}">{{ o.label }}</option>{% endfor %}</datalist>
      {% if seeds %}
      <form method="post" action="/labels/{{ client }}/refs/seed" style="margin-top:16px">
        <p class="muted">{{ seeds|length }} question{{ 's' if seeds|length > 1 }} livrée{{ 's' if seeds|length > 1 }} avec le dépôt (config/reference-questions.yaml). Une fiche peut y être nommée par son numéro (« KB0233 ») : elle est retrouvée dans le catalogue, jamais devinée entre deux. L'import n'écrase jamais une question déjà présente.</p>
        <table><tr><th>Question</th><th>Fiche</th><th>État</th></tr>
        {% for sd in seeds %}<tr><td>{{ sd.text }}</td>
          <td>{% if sd.fiches %}{% for f in sd.fiches %}{{ catalog[f].label if f in catalog else f }}{{ ', ' if not loop.last }}{% endfor %}{% elif sd.why %}{{ sd.expected|join(', ') }}{% else %}<span class="muted">aucune fiche</span>{% endif %}</td>
          <td class="seed-state">{{ sd.state }}{% if sd.why %}<div class="muted">{{ sd.why }}</div>{% endif %}</td></tr>{% endfor %}</table>
        <div class="btns"><button class="b-soft">Importer les questions du dépôt</button></div>
      </form>
      {% endif %}
    </div>
    <div class="card">
      <h2>Questions de référence ({{ refs|length }})</h2>
      <table><tr><th>Question</th><th>Fiche attendue</th><th></th></tr>
      {% for r in refs %}<tr><td>{{ r.text }}<div class="muted">{{ r.labeled_by_name }} — {{ (r.labeled_at or '')[:16].replace('T', ' ') }}{% if r.source == 'seed' %} · dépôt{% endif %}</div></td>
        <td>{% if r.expected_list %}{% for f in r.expected_list %}{{ catalog[f].label if f in catalog else f }}{% if f not in catalog %} <span class="muted">(absente de la carte)</span>{% endif %}{{ ', ' if not loop.last }}{% endfor %}{% else %}<span class="muted">aucune fiche</span>{% endif %}</td>
        <td style="white-space:nowrap"><a href="/labels/{{ client }}/refs?id={{ r.RowKey }}">Modifier</a>
          <form method="post" action="/labels/{{ client }}/refs/{{ r.RowKey }}/delete" style="display:inline"><button class="b-soft" style="padding:3px 8px;font-size:.8rem">Supprimer</button></form></td></tr>
      {% else %}<tr><td colspan="3" class="muted">Aucune question de référence pour l'instant.</td></tr>{% endfor %}</table>
    </div>
  </div>

{% else %}
  <p><a class="muted" href="/labels?client={{ client }}">← Retour</a></p>
  <div class="grid">
    <div class="card">
      <h2>{{ ticket_id }}</h2>
      {% for name, value in fields %}<div class="k">{{ name }}</div><pre class="f">{{ value }}</pre>{% endfor %}
    </div>
    <div class="card">
      <h2>Quelle fiche ce ticket demandait-il ?</h2>
      <p class="muted">Réponse du moteur : <span class="badge eng">{{ kinds.get(row.kefind_kind or 'unseen', row.kefind_kind) }}</span>
        {% if row.kefind_question %}<br>« {{ row.kefind_question }} »{% endif %}</p>
      {% if current is not none %}<div class="msg">Étiquette actuelle : {% if current %}{% for f in current %}{{ catalog[f].label if f in catalog else f }}{{ ', ' if not loop.last }}{% endfor %}{% else %}aucune fiche{% endif %}
        — {{ label.labeled_by_name }}</div>{% elif label and label.skipped %}<div class="msg">Passé par {{ label.labeled_by_name }}.</div>{% endif %}
      {% if shown and shown in catalog %}
      <form method="post" action="/labels/{{ client }}/t/{{ ticket_id }}">
        <input type="hidden" name="seen" value="{{ seen }}">
        <div class="cand shown"><div><span class="badge eng">montrée par le moteur</span><br><b>{{ catalog[shown].label }}</b></div></div>
        <div class="btns"><button class="b-ok" name="decision" value="confirm">Oui, c'est la bonne fiche</button></div>
      </form>
      {% endif %}
      <form method="post" action="/labels/{{ client }}/t/{{ ticket_id }}" style="margin-top:14px">
        <input type="hidden" name="seen" value="{{ seen }}">
        <div class="k">{{ 'Ou une autre :' if shown else 'Candidates du moteur :' }}</div>
        {% for f in candidates %}<label class="cand{{ ' shown' if f == shown }}"><input type="checkbox" name="fiche" value="{{ f }}"> <span>{{ catalog[f].label }}{% if not catalog[f].searchable %} <span class="muted">(non exploitable par le moteur)</span>{% endif %}</span></label>{% endfor %}
        <div class="k" style="margin-top:10px">Autre fiche du KB</div>
        <input type="text" name="other" list="all-fiches" placeholder="Tapez quelques mots du titre…" autocomplete="off">
        <datalist id="all-fiches">{% for o in options %}<option value="{{ o.fiche_id }}">{{ o.label }}</option>{% endfor %}</datalist>
        <div class="btns">
          <button class="b-main" name="decision" value="select">Enregistrer la sélection</button>
          <button class="b-soft" name="decision" value="none">Aucune fiche ne couvre ce ticket</button>
          <button class="b-soft" name="decision" value="skip">Je ne sais pas — passer</button>
        </div>
      </form>
    </div>
  </div>
{% endif %}
</main>
</body>
</html>
"""
