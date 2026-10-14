# =====================================================================
# KnowledgeEngine -- review of the routing proposals ("Revue", routing chantier; spec
# docs/v10-deterministic-engine.md, "Semantic enrichment at ingestion, deterministic routing at runtime")
#
# Nothing routes unless a person validated it. fn-kecore PROPOSES (pass A: an intent, an application
# and short requests per fiche; pass B: the client's closed list of intents; pass C: one intent of that
# list per fiche, with core aliases); this tab is where a person DECIDES:
#   - per fiche: validate (intent, application, aliases, codes) or reject, from a pre-filled form,
#     walking the fiches "à valider" (proposed) or "à revoir" (stale: the document's text changed, the
#     previous decision is shown and can be carried over, the engine re-checking it on the new text);
#   - the taxonomy: freeze it (phase 2 only: L0 numbers/codes and L1 aliases need none);
#   - the routing tables: rebuild + measure (the scoreboard's gate), activate (a passed gate, or an
#     explicit, recorded override when the measure is insufficient), deactivate (a sticky kill switch).
# Decisions apply at the next rebuild; unreviewed fiches simply fall to L3 (proposed by meaning).
#
# Same rules as the dictionary tab (app/dictionary_tab.py), for the same reasons:
#   - the Web App has no Blob role and does not ship kefind: everything goes through fn-kecore's API
#     (app/kecore_client.py), which validates every decision AGAIN with the routing's own normalization
#     (kefind.review.validate_decision). This module checks shapes only and never re-implements
#     normalization: an alias is posted exactly as the reviewer saw or typed it;
#   - claims-only access (label_access, then the client among allowed_clients), same-origin POSTs,
#     messages as codes (never text from the query string), POST/redirect/GET;
#   - a fiche id is a document name (spaces, en dashes, '/'): URLs carry its key (sha256[:32], checked
#     with fullmatch), the id itself travels only in a form body, bound to the key it must hash to.
# Exception to PRG, by design: a taxonomy freeze the engine refuses (400: the row it names; 409: the
# fiches it would orphan) re-renders the form with what was posted, so ~60 edited rows are never lost.
# Labels, aliases and fiche texts come from documents and a model: autoescaped, nothing is |safe.
# =====================================================================
import hashlib
import re
from urllib.parse import urlparse

from flask import Blueprint, abort, redirect, render_template_string, request, url_for

STATUSES = ("proposed", "validated", "rejected", "stale")
QUEUES = ("proposed", "stale")                 # what "next" walks: never-reviewed, then changed fiches
DECISIONS = ("validated", "rejected")
LANGS = ("fr", "en")
ALIAS_SOURCES = ("pass_a", "pass_c", "reference", "manual")
MANUAL_ALIAS_ROWS = 3
BLANK_TAXONOMY_ROWS = 3
MAX_FORM_ROWS = 200          # aliases, references or codes of one form: more is a crafted post
MAX_TAXONOMY_ROWS = 260      # 200 intents (kefind.taxonomy.MAX_TAXONOMY_INTENTS) + blank rows, with slack
MAX_FIELD_CHARS = 1000       # one taxonomy cell; the engine's own limits (120 / 200) are far below
MAX_ALIAS_TEXT = 500         # a promoted reference question may be that long; the engine refuses > 120
MAX_REASON_CHARS = 500
MAX_ERROR_CHARS = 1000
PASS_C_MINUTES = "30 à 50 min"   # pass C on the shared 30 kTPM deployment (design critique, operability #10)

_KEY_RE = re.compile(r"[0-9a-f]{32}")          # always fullmatch: "$" would let a trailing newline in
_SHA_RE = re.compile(r"[0-9a-f]{64}")
_SEEN_RE = re.compile(r"[0-9A-Za-z:+.\-]{0,64}")  # the decided_at the reviewer saw, "" when none
_INTENT_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_APP_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_REF_RE = re.compile(r"ref-[0-9a-f]{24}")
_BUILD_RE = re.compile(r"[0-9A-Za-z-]{1,64}")
_CODE_RE = re.compile(r"[^\x00-\x1f\x7f]{1,100}")
_INDEX_RE = re.compile(r"[0-9]{1,3}")

STATUS_LABELS = {"proposed": "à valider", "validated": "validée", "rejected": "rejetée", "stale": "à revoir"}
GATE_LABELS = {"passed": "réussie", "failed": "échouée", "insufficient": "insuffisante", "invalid": "invalide"}
BUILD_STATES = {"completed": "terminée", "failed": "en échec"}
SOURCE_LABELS = {"pass_a": "passe A", "pass_c": "passe C", "reference": "question de référence",
                 "manual": "saisi"}

# Messages are passed between pages as codes, never as free text (no text injected by a crafted link).
MESSAGES = {
    "validated": "Validé : pris en compte au prochain « Reconstruire et mesurer ».",
    "validated_collision": "Validé — attention : un de ses alias est aussi validé sur une autre fiche ; il ne "
                           "routera vers aucune des deux tant que l'une ne le retire pas (voir « Collisions » "
                           "après « Reconstruire et mesurer »).",
    "rejected": "Rejeté : cette fiche ne sera routée par aucune règle (elle reste proposée par le sens, en L3).",
    "carried": "Décision précédente reconduite sur le nouveau texte : prise en compte au prochain "
               "« Reconstruire et mesurer ».",
    "carried_partial": "Décisions reconduites, sauf certaines que le moteur refuse sur le nouveau texte : "
                       "ouvrez les fiches encore « à revoir » une par une.",
    "carry_failed": "Reconduction refusée : la décision précédente ne passe plus les contrôles sur le nouveau "
                    "texte (alias, code, intention retirée…) : décidez à nouveau ci-dessous.",
    "nothing_stale": "Aucune fiche « à revoir » à reconduire.",
    "done": "Plus aucune fiche dans cette file.",
    "frozen": "Taxonomie figée : la passe C (une intention par fiche) tourne au prochain « Reconstruire et "
              "mesurer ».",
    "run_started": "Reconstruction lancée : les tables seront mesurées par le scoreboard, puis proposées à "
                   "l'activation.",
    "running": "Une reconstruction est déjà en cours : attendez qu'elle se termine.",
    "activated": "Tables activées : chaque instance de /find les prend en moins d'une minute.",
    "activate_refused": "Activation refusée : il faut une mesure « réussie » des tables candidates du dernier "
                        "run — ou, si elle est « insuffisante », cocher la dérogation avec un motif.",
    "reason_missing": "Pour accepter une mesure insuffisante, écrivez un motif : il est enregistré avec "
                      "l'activation.",
    "deactivated": "Routage désactivé : chaque instance cesse de router en moins d'une minute, et il le reste "
                   "jusqu'à la prochaine activation par une personne.",
    "changed": "Cette fiche ou sa décision a changé depuis son affichage : vérifiez-la puis recommencez.",
    "error": "Le moteur a refusé la demande : une valeur est invalide (par exemple un alias de moins de 2 mots "
             "utiles au routage, un chiffre isolé, une adresse e-mail, un code qui n'est pas propre à la fiche).",
    "unavailable": "Le moteur ne répond pas : réessayez dans un instant.",
}


def _same_origin() -> bool:
    src = request.headers.get("Origin") or request.headers.get("Referer") or ""
    return bool(src) and urlparse(src).netloc == request.host


def fiche_key(fiche_id: str) -> str:
    """The key of a fiche (kefind.review.fiche_key, tickets_service.fiche_row_key): an identity hash, not
    a normalization -- the Web App only uses it to bind a posted fiche id to the key of its URL."""
    return hashlib.sha256(fiche_id.encode("utf-8")).hexdigest()[:32]


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _collapse(value) -> str:
    return " ".join(str(value or "").split())


def _status_of(exc) -> int | None:
    """The HTTP status of an engine error (kecore_client.EngineError.status); for an error raised without
    one, read from its historical text ("... -> HTTP 409: ...")."""
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    match = re.search(r"HTTP (\d{3})", str(exc))
    return int(match.group(1)) if match else None


def _code(exc) -> str:
    """The message code of a failed engine call: 409 = changed meanwhile, other 4xx = refused, else
    unreachable (5xx, timeout, connection)."""
    status = _status_of(exc)
    if status == 409:
        return "changed"
    return "error" if status is not None and 400 <= status < 500 else "unavailable"


def _name(item) -> str:
    """A fiche as listed by the engine: {"fiche_id", "label"} or a bare id."""
    if isinstance(item, dict):
        return str(item.get("label") or item.get("fiche_id") or "")
    return str(item)


def _rate(value) -> dict:
    value = _dict(value)
    return {"k": _int(value.get("k")), "n": _int(value.get("n"))}


def _issue_rows(collisions: dict, blocked: dict) -> tuple[list, list]:
    """Collisions and blocked entries of the tables, as display rows {"level", "what", "fiches", "detail"}:
    the engine's shapes (kefind.routing.RoutingTables) differ per level, the page lists them alike."""
    l0 = collisions.get("l0_numbers")
    l0_items = sorted(l0.items()) if isinstance(l0, dict) else [
        (_dict(x).get("number"), _dict(x).get("fiches")) for x in _list(l0)]
    out = [{"level": "L0", "what": f"n° {number}", "fiches": [_name(f) for f in _list(fiches)], "detail": ""}
           for number, fiches in l0_items]
    for item in map(_dict, _list(collisions.get("l1"))):
        out.append({"level": "L1", "what": " ".join(map(str, _list(item.get("tokens")))),
                    "fiches": [_name(f) for f in _list(item.get("fiches"))],
                    "detail": " / ".join(map(str, _list(item.get("aliases"))))})
    for item in map(_dict, _list(collisions.get("l2"))):
        out.append({"level": "L2", "what": f"{item.get('intent') or ''} · {item.get('app') or 'sans application'}",
                    "fiches": [_name(f) for f in _list(item.get("fiches"))], "detail": ""})
    held = []
    for item in map(_dict, _list(blocked.get("l1"))):
        held.append({"level": "L1", "what": " ".join(map(str, _list(item.get("tokens")))),
                     "fiches": [_name(f) for f in _list(item.get("fiches"))], "detail": str(item.get("reason") or "")})
    for item in map(_dict, _list(blocked.get("l2"))):
        pending = [_name(f) for f in _list(item.get("pending"))]
        held.append({"level": "L2", "what": f"{item.get('intent') or ''} · {item.get('app') or 'sans application'}",
                     "fiches": [_name(f) for f in _list(item.get("fiches"))],
                     "detail": ("en attente de revue : " + ", ".join(pending)) if pending else ""})
    return out, held


def overview_view(data, status_filter: str = "") -> dict:
    """The engine's overview (GET kecore/review) made safe for the template: every block present (None or
    empty when the engine left it out), fiches filtered by status, a fiche linked by its key only when the
    key has a key's shape."""
    data = _dict(data)
    routing, taxonomy, counts = _dict(data.get("routing")), _dict(data.get("taxonomy")), _dict(data.get("counts"))
    fiches = []
    for row in _list(data.get("fiches")):
        if not isinstance(row, dict) or (status_filter and row.get("status") != status_filter):
            continue
        key = row.get("key")
        fiches.append({**row, "key": key if isinstance(key, str) and _KEY_RE.fullmatch(key) else None})
    collisions, blocked = _issue_rows(_dict(data.get("collisions")), _dict(data.get("blocked")))
    gate = _dict(routing.get("gate")) or None
    if gate is not None:
        wrong = _dict(gate.get("wrong_exact"))
        gate = {**gate, "wrong_exact": {"k": _int(wrong.get("k")), "n": _int(wrong.get("n")),
                                        "ids": [str(i) for i in _list(wrong.get("ids"))]},
                "route_first": _rate(gate.get("route_first")), "base_first": _rate(gate.get("base_first")),
                "reasons": [str(r) for r in _list(gate.get("reasons"))],
                "excluded": [str(r) for r in _list(gate.get("excluded"))]}
    candidate = _dict(routing.get("candidate")) or None
    if candidate is not None:
        candidate = {**candidate, "stats": _dict(candidate.get("stats"))}
    active = _dict(routing.get("active")) or None
    if active is not None:
        active = {**active, "override": _dict(active.get("override")) or None}
    proposed = _dict(taxonomy.get("proposed")) or None
    return {
        "run_id": str(data.get("run_id") or ""),
        "build": _dict(data.get("build")) or None,
        "running": _dict(data.get("running")) or None,
        "frozen": _dict(taxonomy.get("frozen")) or None,
        "proposed": proposed,
        "counts": {name: _int(counts.get(name)) for name in (*STATUSES, "routable")},
        "fiches": fiches,
        "collisions": collisions,
        "blocked": blocked,
        "candidate": candidate,
        "active": active,
        "effective": routing.get("effective") is True,
        "killed": routing.get("killed") is True,
        "gate": gate,
    }


def fiche_view(data) -> dict:
    """One fiche to review (GET kecore/review/fiche) made safe for the template, the form pre-filled from
    the engine's ``prefill`` (pass C's intent, pass A's application unless inferred, pass C aliases ticked,
    pass A aliases unticked -- or the current decision when there is one)."""
    data = _dict(data)
    prefill = _dict(data.get("prefill"))
    taxonomy = [{"id": str(_dict(t).get("id") or ""), "label_fr": str(_dict(t).get("label_fr") or "")}
                for t in _list(data.get("taxonomy")) if _dict(t).get("id")]
    apps = [str(a) for a in _list(data.get("apps")) if a]
    aliases = []
    for item in map(_dict, _list(prefill.get("aliases"))):
        text = _collapse(item.get("text"))
        if text:
            aliases.append({"text": text, "lang": str(item.get("lang") or ""), "source": str(item.get("source") or ""),
                            "ref_id": str(item.get("ref_id") or ""), "checked": item.get("checked") is True})
    references = []
    for item in map(_dict, _list(data.get("references"))):
        if item.get("ref_id") and item.get("text"):
            references.append({"ref_id": str(item["ref_id"]), "text": _collapse(item["text"]),
                               "tokens": [str(t) for t in _list(item.get("tokens"))],
                               "is_alias": item.get("is_alias") is True})
    intent, app = str(prefill.get("intent") or ""), str(prefill.get("primary_app") or "")
    ids = {t["id"] for t in taxonomy}
    position = _dict(data.get("position"))
    return {
        **data,
        "position": {"index": _int(position.get("index")), "total": _int(position.get("total"))} if position else None,
        "proposal_a": _dict(data.get("proposal_a")) or None,
        "proposal_c": _dict(data.get("proposal_c")) or None,
        "decision": _dict(data.get("decision")) or None,
        "previous_decision": _dict(data.get("previous_decision")) or None,
        "taxonomy": taxonomy,
        "apps": apps,
        "candidate_codes": [str(c) for c in _list(data.get("candidate_codes")) if c],
        "references": references,
        "prefill": {"intent": intent if intent in ids else "", "primary_app": app if app in apps else "",
                    "aliases": aliases, "codes": {str(c) for c in _list(prefill.get("codes"))}},
        # a pre-filled value the lists no longer hold (refrozen taxonomy, removed dictionary entry): said, not posted
        "prefill_unknown": [v for v, ok in ((intent, intent in ids), (app, app in apps)) if v and not ok],
        "seen": str(data.get("seen") or ""),
    }


def _indices(values, limit: int = MAX_FORM_ROWS) -> list[int] | None:
    """The row numbers of ticked checkboxes, sorted and deduplicated; None when one is not a row number."""
    out = set()
    for value in values:
        if not _INDEX_RE.fullmatch(value or "") or int(value) >= limit:
            return None
        out.add(int(value))
    return sorted(out)


def posted_aliases(form) -> list[dict] | None:
    """The aliases the reviewer kept, in the order of the form: the ticked rows (text, language, origin and
    reference exactly as shown), the reference questions promoted, the aliases typed. None when a value is
    not of the expected shape (a crafted post): the engine checks the content itself."""
    out = []
    rows = _indices(form.getlist("alias"))
    refs = _indices(form.getlist("ref"))
    if rows is None or refs is None:
        return None
    for i in rows:
        text = _collapse(form.get(f"alias_text_{i}"))
        lang, source = form.get(f"alias_lang_{i}", ""), form.get(f"alias_source_{i}", "")
        ref_id = form.get(f"alias_ref_{i}", "")
        if not text or len(text) > MAX_ALIAS_TEXT or lang not in LANGS or source not in ALIAS_SOURCES \
                or (ref_id and not _REF_RE.fullmatch(ref_id)):
            return None
        out.append({"text": text, "lang": lang, "source": source, "ref_id": ref_id})
    for j in refs:
        ref_id, text = form.get(f"ref_id_{j}", ""), _collapse(form.get(f"ref_text_{j}"))
        if not _REF_RE.fullmatch(ref_id) or not text or len(text) > MAX_ALIAS_TEXT:
            return None
        out.append({"text": text, "lang": "fr", "source": "reference", "ref_id": ref_id})
    for k in range(MANUAL_ALIAS_ROWS):
        text = _collapse(form.get(f"manual_text_{k}"))
        if not text:
            continue
        lang = form.get(f"manual_lang_{k}") or "fr"
        if len(text) > MAX_ALIAS_TEXT or lang not in LANGS:
            return None
        out.append({"text": text, "lang": lang, "source": "manual", "ref_id": ""})
    return out


def posted_codes(form) -> list[str] | None:
    codes: list[str] = []
    for code in form.getlist("code"):
        if not _CODE_RE.fullmatch(code or ""):
            return None
        if code not in codes:
            codes.append(code)
    return codes if len(codes) <= MAX_FORM_ROWS else None


def taxonomy_source(view: dict, wanted: str) -> str:
    """The taxonomy the form edits: the one asked for when it exists, else the frozen one (a re-freeze
    starts from what routes today), else pass B's proposal; "" when there is neither."""
    available = [name for name in ("frozen", "proposed") if view.get(name)]
    if wanted in available:
        return wanted
    return available[0] if available else ""


def source_rows(view: dict, source: str) -> tuple[list[dict], str]:
    """(rows, source_build) of the taxonomy being edited, each row with its original id (to find its
    members and warnings again after a refused freeze)."""
    taxonomy = _dict(view.get(source)) if source else {}
    rows = []
    for item in map(_dict, _list(taxonomy.get("intents"))):
        fiches = item.get("fiches")
        rows.append({"orig": str(item.get("id") or ""), "id": str(item.get("id") or ""),
                     "label_fr": str(item.get("label_fr") or ""), "description_fr": str(item.get("description_fr") or ""),
                     "delete": False, "n": None,
                     "members": [str(m) for m in _list(item.get("members"))],
                     "fiches": len(fiches) if isinstance(fiches, list) else _int(fiches),
                     "warnings": [str(w) for w in _list(item.get("warnings"))],
                     "origin": str(item.get("origin") or "")})
    build = str((taxonomy.get("build_id") if source == "proposed" else taxonomy.get("source_build")) or "")
    return rows, build if _BUILD_RE.fullmatch(build) else ""


def posted_rows(form) -> list[dict] | None:
    """Every row of the taxonomy form as posted (blank ones included), None on a crafted post."""
    count = form.get("rows", "")
    deleted = _indices(form.getlist("delete"), MAX_TAXONOMY_ROWS) if count.isdigit() else None
    if deleted is None or int(count) > MAX_TAXONOMY_ROWS:
        return None
    rows = []
    for i in range(int(count)):
        values = {name: form.get(f"{name}_{i}", "") for name in ("orig", "id", "label_fr", "description_fr")}
        if any(len(v) > MAX_FIELD_CHARS for v in values.values()):
            return None
        rows.append({**values, "delete": i in deleted, "n": None})
    return rows


def freeze_intents(rows: list[dict]) -> list[dict]:
    """The intents a freeze posts: every row neither deleted nor blank, numbered on the row (``n``) the way
    the engine names a refused one ("entrée 12 (TRANSFERT_APPELS) : …")."""
    intents = []
    for row in rows:
        if row["delete"] or not (row["id"].strip() or row["label_fr"].strip() or row["description_fr"].strip()):
            continue
        intents.append({"id": row["id"].strip(), "label_fr": row["label_fr"].strip(),
                        "description_fr": row["description_fr"].strip()})
        row["n"] = len(intents)
    return intents


def _orphan_rows(orphans) -> list[dict]:
    out = []
    for item in orphans or []:
        if isinstance(item, dict):
            out.append({"name": str(item.get("label") or item.get("fiche_id") or item.get("key") or ""),
                        "intent": str(item.get("intent") or "")})
        else:
            out.append({"name": str(item), "intent": ""})
    return out


def create_review_blueprint(engine, deps: dict):
    """engine: the review_* and routing_* methods of app/kecore_client.EngineClient, or None.
    deps: allowed_clients(), user_id(), display_name(), label_access()."""
    bp = Blueprint("review", __name__)

    def _guard(client=None) -> list:
        if not deps["label_access"]():
            abort(403)
        clients = list(deps["allowed_clients"]())
        if not clients or (client is not None and client not in clients):
            abort(403)
        return clients

    def _post_guard(client) -> None:
        _guard(client)
        if not _same_origin():
            abort(403)
        if engine is None:
            abort(503)

    def _by() -> str:
        return (deps["display_name"]() or "")[:120]

    def _msg():
        return MESSAGES.get(request.args.get("msg", ""))

    def _render(view, http_status=200, **values):
        return render_template_string(
            PAGE, view=view, msg=values.pop("msg", _msg()), display_name=deps["display_name"](),
            status_labels=STATUS_LABELS, gate_labels=GATE_LABELS, build_states=BUILD_STATES,
            source_labels=SOURCE_LABELS, statuses=STATUSES, manual_rows=MANUAL_ALIAS_ROWS,
            pass_c_minutes=PASS_C_MINUTES, **values), http_status

    def _read_overview(client, status=None) -> tuple[dict | None, str | None]:
        if engine is None:
            return None, "Le moteur n'est pas relié à l'application (paramètres KECORE_FUNCTION_URL / KEY)."
        try:
            return engine.review(client, status=status or None), None
        except Exception as exc:
            if _status_of(exc) == 404:
                return None, "Pas encore de carte pour ce client : lancez d'abord un run kecore."
            return None, f"Lecture impossible : {type(exc).__name__}"

    # ------------------------------------------------------------ overview
    @bp.route("/review")
    def home():
        clients = _guard()
        client = request.args.get("client") or clients[0]
        if client not in clients:
            abort(403)
        status = request.args.get("status", "")
        status = status if status in STATUSES else ""
        data, error = _read_overview(client, status)
        return _render("home", clients=clients, client=client, status=status, error=error,
                       d=overview_view(data, status) if data is not None else None)

    @bp.route("/review/<client>/next")
    def next_fiche(client):
        """The next fiche of a queue (the engine's order: fiches reference questions expect, then those in
        collisions or blocked, then by label), or back to the overview when the queue is empty."""
        _guard(client)
        status = request.args.get("status") or "proposed"
        if status not in QUEUES:
            abort(400)
        msg = request.args.get("msg") if request.args.get("msg") in MESSAGES else None
        if engine is None:
            return redirect(url_for("review.home", client=client))
        try:
            key = _dict(engine.review_next(client, status)).get("key")
        except Exception as exc:
            return redirect(url_for("review.home", client=client, status=status, msg=_code(exc)))
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            return redirect(url_for("review.home", client=client, status=status, msg=msg or "done"))
        return redirect(url_for("review.fiche", client=client, key=key, msg=msg))

    # ------------------------------------------------------------ one fiche
    @bp.route("/review/<client>/f/<key>")
    def fiche(client, key):
        _guard(client)
        if not _KEY_RE.fullmatch(key):
            abort(404)
        data, error = None, None
        if engine is None:
            error = "Le moteur n'est pas relié à l'application (paramètres KECORE_FUNCTION_URL / KEY)."
        else:
            try:
                data = engine.review_fiche(client, key)
            except Exception as exc:
                if _status_of(exc) == 404:
                    abort(404)
                error = f"Lecture impossible : {type(exc).__name__}"
        f = fiche_view(data) if data is not None else None
        queue = "stale" if f and f.get("status") == "stale" else "proposed"
        return _render("fiche", client=client, key=key, f=f, queue=queue, error=error)

    @bp.route("/review/<client>/f/<key>", methods=["POST"])
    def decide(client, key):
        _post_guard(client)
        if not _KEY_RE.fullmatch(key):
            abort(404)
        form = request.form
        status = form.get("decision", "")
        fiche_id = form.get("fiche_id", "")
        source, seen = form.get("source_sha256", ""), form.get("seen", "")
        intent, app = form.get("intent", ""), form.get("primary_app", "")
        if status not in DECISIONS or not fiche_id or fiche_key(fiche_id) != key \
                or not _SHA_RE.fullmatch(source) or not _SEEN_RE.fullmatch(seen) \
                or (intent and not _INTENT_RE.fullmatch(intent)) or (app and not _APP_RE.fullmatch(app)):
            abort(400)
        aliases, codes = posted_aliases(form), posted_codes(form)
        if aliases is None or codes is None:
            abort(400)
        body = {"client": client, "by": _by(), "fiche_id": fiche_id, "source_sha256": source, "seen": seen,
                "status": status, "intent": intent, "primary_app": app, "aliases": aliases, "codes": codes,
                "note": form.get("note", "")}
        by_id = (deps["user_id"]() or "")[:64]
        if by_id:
            body["by_id"] = by_id
        queue = form.get("queue") if form.get("queue") in QUEUES else "proposed"
        try:
            result = _dict(engine.review_decide(body))
        except Exception as exc:
            return redirect(url_for("review.fiche", client=client, key=key, msg=_code(exc)))
        code = "validated_collision" if status == "validated" and _list(result.get("collisions")) else status
        return redirect(url_for("review.next_fiche", client=client, status=queue, msg=code))

    @bp.route("/review/<client>/f/<key>/carry", methods=["POST"])
    def carry_one(client, key):
        """A stale fiche: its previous decision posted again on the new text (the engine re-validates it)."""
        _post_guard(client)
        if not _KEY_RE.fullmatch(key):
            abort(404)
        try:
            result = _dict(engine.review_carry({"client": client, "by": _by(), "keys": [key]}))
        except Exception as exc:
            return redirect(url_for("review.fiche", client=client, key=key, msg=_code(exc)))
        if _list(result.get("failed")) or not _int(result.get("carried")):
            return redirect(url_for("review.fiche", client=client, key=key, msg="carry_failed"))
        return redirect(url_for("review.next_fiche", client=client, status="stale", msg="carried"))

    @bp.route("/review/<client>/carry", methods=["POST"])
    def carry_all(client):
        """Every fiche the engine lists as stale right now (read again, never taken from the form)."""
        _post_guard(client)
        try:
            data = overview_view(engine.review(client, status="stale"), "stale")
        except Exception as exc:
            return redirect(url_for("review.home", client=client, msg=_code(exc)))
        keys = list(dict.fromkeys(f["key"] for f in data["fiches"] if f["key"]))
        if not keys:
            return redirect(url_for("review.home", client=client, msg="nothing_stale"))
        try:
            result = _dict(engine.review_carry({"client": client, "by": _by(), "keys": keys}))
        except Exception as exc:
            return redirect(url_for("review.home", client=client, status="stale", msg=_code(exc)))
        code = "carried_partial" if _list(result.get("failed")) else "carried"
        return redirect(url_for("review.home", client=client, status="stale", msg=code))

    # ------------------------------------------------------------ taxonomy
    def _taxonomy_page(client, view, source, source_build, rows, *, status=200, error=None, orphans=(),
                       msg=None):
        """The freeze form: ``rows`` as given (a refused freeze shows what was posted, numbered the way the
        engine names its rows), their members and warnings found again by original id, blank rows after."""
        info = {r["orig"]: r for r in source_rows(view, source)[0]} if source else {}
        shown = []
        for row in rows:
            extra = info.get(row.get("orig") or "", {})
            shown.append({**row, "members": extra.get("members", []), "fiches": extra.get("fiches", 0),
                          "warnings": extra.get("warnings", []), "origin": extra.get("origin", "")})
        shown += [{"orig": "", "id": "", "label_fr": "", "description_fr": "", "delete": False, "n": None,
                   "members": [], "fiches": 0, "warnings": [], "origin": ""}] * BLANK_TAXONOMY_ROWS
        frozen = view.get("frozen") or {}
        base_version = frozen.get("version") if isinstance(frozen.get("version"), int) else 0
        return _render("taxonomy", http_status=status, client=client, d=view, tax_source=source, source_build=source_build,
                       base_version=base_version,
                       rows=shown, error=error, orphans=_orphan_rows(orphans), msg=msg if msg is not None else _msg(),
                       calls=view["counts"]["routable"] or "180")

    @bp.route("/review/<client>/taxonomy")
    def taxonomy(client):
        _guard(client)
        data, error = _read_overview(client)
        view = overview_view(data)
        source = taxonomy_source(view, request.args.get("source", ""))
        rows, source_build = source_rows(view, source)
        return _taxonomy_page(client, view, source, source_build, rows, error=error)

    @bp.route("/review/<client>/taxonomy", methods=["POST"])
    def freeze(client):
        _post_guard(client)
        form = request.form
        action, source, source_build = form.get("decision", ""), form.get("source", ""), form.get("source_build", "")
        if action not in ("freeze", "as_is") or source not in ("proposed", "frozen") \
                or (source_build and not _BUILD_RE.fullmatch(source_build)) \
                or (action == "as_is" and source != "proposed"):
            abort(400)
        if action == "as_is":  # the proposal as the engine holds it now, never the form's copy of it
            try:
                fresh = overview_view(engine.review(client))
            except Exception as exc:
                return redirect(url_for("review.taxonomy", client=client, msg=_code(exc)))
            rows, build = source_rows(fresh, source)
            if not rows or build != source_build:
                return redirect(url_for("review.taxonomy", client=client, msg="changed"))
        else:
            rows = posted_rows(form)
            if rows is None:
                abort(400)
        intents = freeze_intents(rows)
        base = form.get("base_version", "")
        if not base.isdigit() or len(base) > 6:
            abort(400)
        # the frozen version this form was built from: a freeze made by someone else meanwhile is never overwritten
        body = {"client": client, "by": _by(), "source_build": source_build, "intents": intents,
                "base_version": int(base)}
        if form.get("orphans_ok") == "1":
            body["orphans_ok"] = True
        try:
            engine.review_taxonomy(body)
        except Exception as exc:
            status = _status_of(exc)
            if status not in (400, 409):
                return redirect(url_for("review.taxonomy", client=client, source=source, msg=_code(exc)))
            try:  # the members and warnings of each row, shown again next to what was posted
                view = overview_view(engine.review(client))
            except Exception:
                view = overview_view(None)
            message = (getattr(exc, "error", "") or "")[:MAX_ERROR_CHARS] or MESSAGES["error"]
            kept = [r for r in rows if r["n"] is not None or r["delete"]]
            return _taxonomy_page(client, view, source, source_build, kept, status=status, error=message,
                                  orphans=getattr(exc, "orphans", []) if status == 409 else [], msg="")
        return redirect(url_for("review.home", client=client, msg="frozen"))

    # ------------------------------------------------------------ routing tables
    @bp.route("/review/<client>/routing/run", methods=["POST"])
    def routing_run(client):
        """Rebuild the tables and measure them. The 202 answer carries the durable system key in its
        URLs: it is dropped here, never rendered."""
        _post_guard(client)
        try:
            engine.routing_run({"client": client, "measure": True})
        except Exception as exc:
            code = "running" if _status_of(exc) == 409 else _code(exc)
            return redirect(url_for("review.home", client=client, msg=code))
        return redirect(url_for("review.home", client=client, msg="run_started"))

    @bp.route("/review/<client>/routing/activate", methods=["POST"])
    def routing_activate(client):
        _post_guard(client)
        build_id = request.form.get("build_id", "")
        if not _BUILD_RE.fullmatch(build_id):
            abort(400)
        body = {"client": client, "build_id": build_id, "by": _by()}
        if request.form.get("accept_insufficient") == "1":
            reason = _collapse(request.form.get("reason"))
            if not reason:
                return redirect(url_for("review.home", client=client, msg="reason_missing"))
            if len(reason) > MAX_REASON_CHARS:
                abort(400)
            body.update(accept_insufficient=True, reason=reason)
        try:
            engine.routing_activate(body)
        except Exception as exc:
            code = "activate_refused" if _status_of(exc) == 409 else _code(exc)
            return redirect(url_for("review.home", client=client, msg=code))
        return redirect(url_for("review.home", client=client, msg="activated"))

    @bp.route("/review/<client>/routing/deactivate", methods=["POST"])
    def routing_deactivate(client):
        _post_guard(client)
        try:
            engine.routing_deactivate({"client": client, "by": _by()})
        except Exception as exc:
            return redirect(url_for("review.home", client=client, msg=_code(exc)))
        return redirect(url_for("review.home", client=client, msg="deactivated"))

    return bp


PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{% if view == "home" and d and d.running %}<meta http-equiv="refresh" content="30">{% endif %}
<title>KnowledgeEngine — Revue</title>
<style>
  :root{--bg:#f7f4ef;--panel:#fff;--panel-2:#faf3ea;--line:#e8ddd0;--txt:#20211f;--muted:#7a7267;
    --accent:#e2703a;--accent-tint:#fbe9dc;--blue:#5c85cf;--blue-tint:#e9f0fb;--ok:#1f9d76;--ok-tint:#e3f5ee;
    --warn:#8a6a3f;--warn-tint:#fdf0c6;--danger:#b3372a;--danger-tint:#fbe4e0;--btn:#14172a}
  *{box-sizing:border-box}
  body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt);line-height:1.55}
  header{padding:14px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;background:var(--panel-2);flex-wrap:wrap}
  header h1{margin:0;font-size:1.02rem;font-weight:650}
  header nav{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
  header nav a{color:var(--txt);text-decoration:none;font-size:.85rem}
  header nav a.on{font-weight:650;border-bottom:2px solid var(--accent)}
  .who{margin-left:auto;color:var(--muted);font-size:.8rem}
  main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px;overflow-x:auto}
  .card h2{margin:0 0 10px;font-size:.98rem}
  .grid{display:grid;grid-template-columns:1.2fr 1fr;gap:16px}
  @media (max-width:820px){.grid{grid-template-columns:1fr}}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
  .stat{background:var(--panel-2);border-radius:8px;padding:10px 12px;color:var(--txt);text-decoration:none}
  .stat b{display:block;font-size:1.3rem}
  .stat span{color:var(--muted);font-size:.78rem}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:.86rem;vertical-align:top}
  th{color:var(--muted);font-size:.76rem;text-transform:uppercase;letter-spacing:.03em}
  .muted{color:var(--muted);font-size:.85rem}
  .msg{padding:10px 12px;border-radius:8px;background:var(--blue-tint);margin-bottom:14px;font-size:.86rem}
  .err{background:var(--danger-tint);color:var(--danger)}
  .ok{background:var(--ok-tint);color:var(--ok)}
  .warn{background:var(--warn-tint);color:var(--warn)}
  pre.f{white-space:pre-wrap;font-family:inherit;font-size:.86rem;background:var(--panel-2);padding:10px;border-radius:8px;margin:4px 0 12px;overflow-wrap:anywhere;max-height:360px;overflow-y:auto}
  .k{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.03em;margin-top:10px}
  .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.75rem;font-weight:600;background:var(--blue-tint);color:var(--blue)}
  .s-validated,.g-passed{background:var(--ok-tint);color:var(--ok)}
  .s-rejected{background:var(--line);color:var(--muted)}
  .s-stale,.g-insufficient,.badge.warn{background:var(--warn-tint);color:var(--warn)}
  .g-failed,.g-invalid,.badge.err{background:var(--danger-tint);color:var(--danger)}
  .opt{display:flex;gap:8px;align-items:flex-start;padding:6px 8px;border:1px solid var(--line);border-radius:8px;margin:4px 0}
  .btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;align-items:flex-start}
  form.inline{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:0}
  button,.btn{font:inherit;font-weight:650;border:0;border-radius:8px;padding:8px 13px;cursor:pointer;text-decoration:none;display:inline-block;font-size:.86rem}
  button[disabled]{opacity:.5;cursor:not-allowed}
  .b-ok{background:var(--ok);color:#fff}.b-main{background:var(--btn);color:#fff}
  .b-soft{background:var(--line);color:var(--txt)}.b-danger{background:var(--danger);color:#fff}
  input[type=text],select,textarea{width:100%;font:inherit;font-size:.86rem;padding:7px;border:1px solid var(--line);border-radius:8px;background:#fff}
  .row3{display:grid;grid-template-columns:1fr 80px;gap:6px;margin:4px 0}
  ul.tight{margin:4px 0;padding-left:18px;font-size:.86rem}
  a{color:var(--txt)}
</style>
</head>
<body>
<header>
  <h1>KnowledgeEngine — Revue</h1>
  <nav><a href="/diag">Assistant</a><a href="/itsm">Tickets ITSM</a><a href="/labels">Labellisation</a><a href="/dictionary">Dictionnaire</a><a class="on" href="/review">Revue</a></nav>
  <span class="who">{{ display_name }}</span>
</header>
<main>
{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
{% if error %}<div class="msg err">{{ error }}</div>{% endif %}

{% if view == "home" %}
  {% if clients|length > 1 %}
  <form method="get" action="/review" class="card"><label class="k">Client
    <select name="client" onchange="this.form.submit()">{% for c in clients %}<option value="{{ c }}" {{ 'selected' if c == client }}>{{ c }}</option>{% endfor %}</select></label></form>
  {% endif %}
  {% if d %}
  {% if d.running %}<div class="msg warn">Reconstruction en cours depuis {{ d.running.started_at }} ({{ d.running.kind }}, {{ d.running.build_id }}) — la page se recharge toutes les 30 s.</div>
  {% elif d.build %}<div class="msg {{ 'err' if d.build.state == 'failed' }}">Dernière reconstruction : {{ d.build.build_id }} (run {{ d.build.run_id }}) — {{ build_states.get(d.build.state, d.build.state) }}.</div>{% endif %}

  {% if d.killed %}<div class="msg err"><b>Routage désactivé</b> par l'interrupteur : /find répond sans routage. Seule une activation par une personne le rallume ; une reconstruction ne le rallume jamais.</div>
  {% elif d.active and not d.effective %}<div class="msg err"><b>Tables actives mais non appliquées</b> : elles ont été construites pour le run {{ d.active.run_id }}, la carte servie est {{ d.run_id or '—' }}. /find répond sans routage tant que de nouvelles tables ne sont pas reconstruites, mesurées et activées.</div>
  {% elif d.active %}<div class="msg ok"><b>Routage actif</b> : tables {{ d.active.build_id }} (run {{ d.active.run_id }}), activées par {{ d.active.activated_by }} le {{ d.active.activated_at }}.{% if d.active.override %} Activées malgré une mesure insuffisante — motif : « {{ d.active.override.reason }} » ({{ d.active.override.by }}).{% endif %}</div>
  {% else %}<div class="msg">Routage inactif : /find répond exactement comme avant (aucune route). Une fiche n'est routée qu'après validation, reconstruction, mesure et activation.</div>{% endif %}

  <div class="card">
    <h2>Fiches du run {{ d.run_id or '—' }}</h2>
    <div class="stats">
      {% for s in statuses %}<a class="stat" href="/review?client={{ client }}&amp;status={{ s }}"><b>{{ d.counts[s] }}</b><span>{{ status_labels[s] }}</span></a>{% endfor %}
      <div class="stat"><b>{{ d.counts.routable }}</b><span>fiches routables</span></div>
    </div>
    <div class="btns">
      <a class="btn b-main" href="/review/{{ client }}/next?status=proposed">Valider la fiche suivante →</a>
      <a class="btn b-soft" href="/review/{{ client }}/next?status=stale">Fiche « à revoir » suivante →</a>
      {% if d.counts.stale %}<form class="inline" method="post" action="/review/{{ client }}/carry" onsubmit="return confirm('Reconduire la décision précédente de chaque fiche « à revoir » ? Le moteur la revérifie sur le nouveau texte.')">
        <button class="b-soft">Reconduire tous les « à revoir » ({{ d.counts.stale }})</button></form>{% endif %}
      <a class="btn b-soft" href="/review/{{ client }}/taxonomy">Taxonomie des intentions →</a>
    </div>
    <p class="muted">Les décisions s'appliquent au prochain « Reconstruire et mesurer ». Les fiches non revues ne sont pas perdues :
      elles tombent simplement en L3 (proposées par le sens, à confirmer). Phase 1 — numéro de fiche, codes, alias — ne demande ni
      taxonomie ni passe C ; phase 2 — intention + application — demande une taxonomie figée.</p>
  </div>

  <div class="card">
    <h2>Fiches{% if status %} : {{ status_labels[status] }}{% endif %} ({{ d.fiches|length }})</h2>
    <p class="muted"><a href="/review?client={{ client }}">toutes</a>{% for s in statuses %} · <a href="/review?client={{ client }}&amp;status={{ s }}">{{ status_labels[s] }}</a>{% endfor %}</p>
    <table><tr><th>Fiche</th><th>Statut</th><th>Intention</th><th>Application</th></tr>
    {% for f in d.fiches %}<tr>
      <td>{% if f.key %}<a href="/review/{{ client }}/f/{{ f.key }}">{{ f.label or f.fiche_id }}</a>{% else %}{{ f.label or f.fiche_id }}{% endif %}
        {% if f.referenced %}<span class="badge">question de référence</span>{% endif %}
        {% if f.error %}<div class="muted">proposition en erreur : {{ f.error|string|truncate(120) }}</div>{% endif %}</td>
      <td><span class="badge s-{{ f.status }}">{{ status_labels.get(f.status, f.status) }}</span></td>
      <td class="muted">A : {{ f.intent_a or '—' }}<br>C : {{ f.intent_c or '—' }}<br><b>validée : {{ f.intent or '—' }}</b></td>
      <td>{{ f.primary_app or '—' }}{% if f.app_inferred %} <span class="badge warn">déduite — à valider</span>{% endif %}</td></tr>
    {% else %}<tr><td colspan="4" class="muted">Aucune fiche.</td></tr>{% endfor %}</table>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Collisions ({{ d.collisions|length }})</h2>
      <p class="muted">Un même numéro, une même formulation ou une même clé (intention, application) validée sur plusieurs fiches ne route vers aucune : gardez-la sur une seule.</p>
      <table>{% for c in d.collisions %}<tr><td>{{ c.level }}</td><td><b>{{ c.what }}</b>{% if c.detail %}<div class="muted">{{ c.detail }}</div>{% endif %}</td><td>{{ c.fiches|join(' · ') }}</td></tr>
      {% else %}<tr><td class="muted">Aucune collision dans les dernières tables.</td></tr>{% endfor %}</table>
    </div>
    <div class="card">
      <h2>Bloqués en attente de revue ({{ d.blocked|length }})</h2>
      <p class="muted">Une formulation ou une clé encore proposée sur une fiche non revue (ou « à revoir ») bloque la route : revoyez ces fiches.</p>
      <table>{% for c in d.blocked %}<tr><td>{{ c.level }}</td><td><b>{{ c.what }}</b>{% if c.detail %}<div class="muted">{{ c.detail }}</div>{% endif %}</td><td>{{ c.fiches|join(' · ') }}</td></tr>
      {% else %}<tr><td class="muted">Rien de bloqué.</td></tr>{% endfor %}</table>
    </div>
  </div>

  <div class="card">
    <h2>Tables de routage</h2>
    <div class="grid">
      <div>
        <div class="k">Tables candidates</div>
        {% if d.candidate %}<p>Build {{ d.candidate.build_id }} · run {{ d.candidate.run_id }}<br>
          <span class="muted">L0 numéros : {{ d.candidate.stats.l0_numbers or 0 }} · L0 codes : {{ d.candidate.stats.l0_codes or 0 }} · L1 alias : {{ d.candidate.stats.l1 or 0 }} · L2 clés : {{ d.candidate.stats.l2 or 0 }}</span></p>
        {% else %}<p class="muted">Aucune : lancez « Reconstruire et mesurer ».</p>{% endif %}
        <div class="k">Mesure (scoreboard)</div>
        {% if d.gate %}<p><span class="badge g-{{ d.gate.status }}">{{ gate_labels.get(d.gate.status, d.gate.status) }}</span>
          <span class="muted">{{ d.gate.sb_id }}</span><br>
          Fiche fausse montrée comme exacte : <b>{{ d.gate.wrong_exact.k }} / {{ d.gate.wrong_exact.n }}</b><br>
          Bonne fiche en premier : <b>{{ d.gate.route_first.k }} / {{ d.gate.route_first.n }}</b> avec routage, {{ d.gate.base_first.k }} / {{ d.gate.base_first.n }} sans<br>
          <span class="muted">{{ d.gate.compared or 0 }} questions comparées · {{ d.gate.excluded|length }} écartées (vues par le relecteur en écrivant un alias)</span></p>
          {% if d.gate.reasons %}<ul class="tight">{% for r in d.gate.reasons %}<li>{{ r }}</li>{% endfor %}</ul>{% endif %}
        {% else %}<p class="muted">Pas encore mesurées.</p>{% endif %}
      </div>
      <div>
        <div class="k">Tables actives</div>
        {% if d.active %}<p>Build {{ d.active.build_id }} · run {{ d.active.run_id }}{% if d.active.carried_from %}<br><span class="muted">reconduites depuis {{ d.active.carried_from }}</span>{% endif %}</p>
        {% else %}<p class="muted">Aucune.</p>{% endif %}
      </div>
    </div>
    <div class="btns">
      <form class="inline" method="post" action="/review/{{ client }}/routing/run"><button class="b-main" {{ 'disabled' if d.running }}>Reconstruire et mesurer</button></form>
      {% if d.candidate %}<form class="inline" method="post" action="/review/{{ client }}/routing/activate">
        <input type="hidden" name="build_id" value="{{ d.candidate.build_id }}">
        <label class="muted"><input type="checkbox" name="accept_insufficient" value="1"> accepter une mesure insuffisante</label>
        <input type="text" name="reason" maxlength="500" placeholder="motif (obligatoire si la case est cochée)" style="max-width:320px">
        <button class="b-ok">Activer</button></form>{% endif %}
      {% if d.active and not d.killed %}<form class="inline" method="post" action="/review/{{ client }}/routing/deactivate" onsubmit="return confirm('Désactiver le routage ? /find répondra sans routage jusqu’à la prochaine activation.')">
        <button class="b-danger">Désactiver</button></form>{% endif %}
    </div>
    <p class="muted">L'activation demande une mesure « réussie » des tables candidates sur le dernier run — ou, si elle est « insuffisante »
      (trop peu de questions mesurables), une dérogation explicite dont le motif est enregistré. La désactivation atteint chaque instance
      en moins d'une minute et reste en place jusqu'à la prochaine activation par une personne.</p>
  </div>
  {% endif %}

{% elif view == "fiche" %}
  <p><a class="muted" href="/review?client={{ client }}">← Vue d'ensemble</a></p>
  {% if f %}
  <div class="grid">
    <div class="card">
      <h2>{{ f.label or f.fiche_id }}</h2>
      <p class="muted">{{ f.fiche_id }} · <span class="badge s-{{ f.status }}">{{ status_labels.get(f.status, f.status) }}</span>
        {% if f.position %} · fiche {{ f.position.index }} / {{ f.position.total }}{% endif %} · run {{ f.run_id }}</p>
      {% if f.l0_number %}<p>Numéro de fiche <b>{{ f.l0_number }}</b> : il route seul (L0) tant que la fiche n'est pas rejetée et que ce numéro ne désigne qu'elle.</p>{% endif %}
      <div class="k">Extrait du texte</div><pre class="f">{{ f.text_excerpt }}</pre>
      {% set a = f.proposal_a %}
      <div class="k">Proposition de la passe A</div>
      {% if a %}
        {% if a.error %}<p class="msg err">{{ a.error }}</p>{% endif %}
        <p>Intention : <b>{{ a.canonical_intent or '—' }}</b>{% if a.intent_label_fr %} — {{ a.intent_label_fr }}{% endif %}<br>
          Application : <b>{{ a.primary_app or '—' }}</b>{% if a.app_inferred %} <span class="badge warn">déduite — à valider</span>{% endif %}
          {% if a.supported_apps %}<span class="muted"> (aussi : {{ a.supported_apps|join(', ') }})</span>{% endif %}</p>
        {% if a.app_evidence %}<ul class="tight">{% for e in a.app_evidence %}<li>{{ e.app }} : « {{ e.quote }} »</li>{% endfor %}</ul>{% endif %}
        {% if a.trigger_keywords %}<p class="muted">Mots déclencheurs (lecture seule, ne routent jamais seuls) : {{ a.trigger_keywords|join(', ') }}</p>{% endif %}
        {% if a.dropped %}<details><summary class="muted">{{ a.dropped|length }} élément(s) écarté(s) par le code</summary><ul class="tight">{% for x in a.dropped %}<li>{{ x.field }} « {{ x.text }} » : {{ x.reason }}</li>{% endfor %}</ul></details>{% endif %}
      {% else %}<p class="muted">Aucune.</p>{% endif %}
      {% set c = f.proposal_c %}
      <div class="k">Proposition de la passe C</div>
      {% if c %}
        {% if c.error %}<p class="msg err">{{ c.error }}</p>{% endif %}
        <p>Intention : <b>{{ c.intent or '—' }}</b></p>
        {% if c.dropped %}<details><summary class="muted">{{ c.dropped|length }} élément(s) écarté(s) par le code</summary><ul class="tight">{% for x in c.dropped %}<li>{{ x.field }} « {{ x.text }} » : {{ x.reason }}</li>{% endfor %}</ul></details>{% endif %}
      {% else %}<p class="muted">Aucune (pas de taxonomie figée, ou passe C pas encore passée).</p>{% endif %}
      {% for title, dec in (('Décision actuelle', f.decision), ('Décision précédente (sur un autre texte de la fiche)', f.previous_decision)) %}{% if dec %}
        <div class="k">{{ title }}</div>
        <p>{{ status_labels.get(dec.status, dec.status) }} par {{ dec.decided_by }} le {{ dec.decided_at }}<br>
          <span class="muted">intention {{ dec.intent or '—' }} · application {{ dec.primary_app or '—' }}{% if dec.codes %} · codes {{ dec.codes|join(', ') }}{% endif %}</span>
          {% if dec.aliases %}<br><span class="muted">alias : {% for x in dec.aliases %}« {{ x.text }} »{{ ', ' if not loop.last }}{% endfor %}</span>{% endif %}
          {% if dec.note %}<br><span class="muted">note : {{ dec.note }}</span>{% endif %}</p>
      {% endif %}{% endfor %}
      {% if f.status == 'stale' and f.previous_decision %}
      <form method="post" action="/review/{{ client }}/f/{{ key }}/carry"><div class="btns"><button class="b-soft">Reconduire la décision précédente</button></div></form>
      <p class="muted">Le moteur la revérifie sur le nouveau texte ; refusée, elle est à refaire ci-contre.</p>{% endif %}
    </div>

    <div class="card">
      <h2>Décision</h2>
      <form method="post" action="/review/{{ client }}/f/{{ key }}">
        <input type="hidden" name="fiche_id" value="{{ f.fiche_id }}">
        <input type="hidden" name="source_sha256" value="{{ f.source_sha256 }}">
        <input type="hidden" name="seen" value="{{ f.seen }}">
        <input type="hidden" name="queue" value="{{ queue }}">
        {% if f.prefill_unknown %}<p class="msg warn">Proposé mais absent des listes actuelles (taxonomie refigée, dictionnaire modifié) : {{ f.prefill_unknown|join(', ') }}.</p>{% endif %}
        <div class="k">Intention</div>
        {% if f.taxonomy %}<select name="intent"><option value="">— aucune intention —</option>{% for t in f.taxonomy %}<option value="{{ t.id }}" {{ 'selected' if t.id == f.prefill.intent }}>{{ t.id }} — {{ t.label_fr }}</option>{% endfor %}</select>
        {% else %}<p class="muted">Aucune taxonomie figée : l'intention reste vide (la phase 1 — numéro, codes, alias — n'en a pas besoin).</p><input type="hidden" name="intent" value="">{% endif %}
        <div class="k">Application principale</div>
        <select name="primary_app"><option value="">— aucune application —</option>{% for app in f.apps %}<option value="{{ app }}" {{ 'selected' if app == f.prefill.primary_app }}>{{ app }}</option>{% endfor %}</select>
        <div class="k">Alias (ce qu'un technicien tape ; 2 à 12 mots)</div>
        {% for x in f.prefill.aliases %}<label class="opt"><input type="checkbox" name="alias" value="{{ loop.index0 }}" {{ 'checked' if x.checked }}>
          <span>{{ x.text }} <span class="muted">({{ x.lang }}, {{ source_labels.get(x.source, x.source) }})</span></span></label>
          <input type="hidden" name="alias_text_{{ loop.index0 }}" value="{{ x.text }}"><input type="hidden" name="alias_lang_{{ loop.index0 }}" value="{{ x.lang }}">
          <input type="hidden" name="alias_source_{{ loop.index0 }}" value="{{ x.source }}"><input type="hidden" name="alias_ref_{{ loop.index0 }}" value="{{ x.ref_id }}">
        {% else %}<p class="muted">Aucun alias proposé.</p>{% endfor %}
        {% if f.references %}<div class="k">Questions de référence qui attendent cette fiche</div>
        {% for r in f.references %}<div class="opt">{% if r.is_alias %}<span>{{ r.text }} <span class="badge">déjà un alias</span></span>
          {% else %}<label><input type="checkbox" name="ref" value="{{ loop.index0 }}"> {{ r.text }} <span class="muted">(promouvoir comme alias)</span></label>
          <input type="hidden" name="ref_id_{{ loop.index0 }}" value="{{ r.ref_id }}"><input type="hidden" name="ref_text_{{ loop.index0 }}" value="{{ r.text }}">{% endif %}
          {% if r.tokens %}<span class="muted"> — lu par le routage : {{ r.tokens|join(' · ') }}</span>{% endif %}</div>{% endfor %}
        <p class="muted">Une question promue ne compte plus dans la mesure « bonne fiche en premier » (elle a servi à écrire la règle) ; une fiche fausse montrée sur elle compte toujours.</p>{% endif %}
        <div class="k">Ajouter un alias</div>
        {% for k in range(manual_rows) %}<div class="row3"><input type="text" name="manual_text_{{ k }}" maxlength="120" placeholder="attribuer ligne teams" autocomplete="off">
          <select name="manual_lang_{{ k }}"><option value="fr">fr</option><option value="en">en</option></select></div>{% endfor %}
        {% if f.candidate_codes %}<div class="k">Codes propres à cette fiche (L0)</div>
        {% for code in f.candidate_codes %}<label class="opt"><input type="checkbox" name="code" value="{{ code }}" {{ 'checked' if code in f.prefill.codes }}> {{ code }}</label>{% endfor %}{% endif %}
        <div class="k">Note</div><textarea name="note" maxlength="500" rows="2">{{ f.decision.note if f.decision else '' }}</textarea>
        <div class="btns"><button class="b-ok" name="decision" value="validated">Valider</button>
          <button class="b-soft" name="decision" value="rejected">Rejeter : rien ne doit router vers cette fiche</button></div>
        <p class="muted">Pris en compte au prochain « Reconstruire et mesurer ». Le moteur revérifie tout : un alias doit garder au moins 2 mots utiles au routage, sans chiffre ni lettre isolés.</p>
      </form>
    </div>
  </div>
  {% endif %}

{% elif view == "taxonomy" %}
  <p><a class="muted" href="/review?client={{ client }}">← Vue d'ensemble</a></p>
  <div class="card">
    <h2>Taxonomie des intentions de {{ client }}</h2>
    {% if d.frozen %}<p>Figée : version {{ d.frozen.version }} ({{ d.frozen.intents|length }} intentions), par {{ d.frozen.frozen_by }} le {{ d.frozen.frozen_at }}.</p>
    {% else %}<p>Aucune taxonomie figée : la phase 1 (numéro de fiche, codes, alias) fonctionne sans elle ; la phase 2 (intention + application) en a besoin.</p>{% endif %}
    {% if d.proposed %}<p class="muted">Proposition de la passe B : build {{ d.proposed.build_id }}, run {{ d.proposed.run_id }} ({{ d.proposed.intents|length }} intentions{% if d.proposed.source %}, à partir de {{ d.proposed.source.intents }} intentions de la passe A sur {{ d.proposed.source.fiches }} fiches{% endif %}).</p>
      {% if d.proposed.source and d.proposed.source.enrichment_errors %}<p class="msg warn">{{ d.proposed.source.enrichment_errors }} fiche(s) sans proposition de la passe A (erreurs) : elles manquent à cette proposition. Relancez d'abord un run kecore (record) : seuls les appels manquants sont refaits.</p>{% endif %}
      {% if d.proposed.error %}<p class="msg err">{{ d.proposed.error }}</p>{% endif %}
      {% if d.proposed.warnings %}<ul class="tight">{% for w in d.proposed.warnings %}<li>{{ w }}</li>{% endfor %}</ul>{% endif %}{% endif %}
    {% if d.frozen and d.proposed %}<p class="muted">Modifier : <a href="/review/{{ client }}/taxonomy?source=frozen">la taxonomie figée</a> · <a href="/review/{{ client }}/taxonomy?source=proposed">la proposition de la passe B</a></p>{% endif %}
  </div>
  {% if orphans %}<div class="card"><h2>Fiches qui perdraient leur intention validée ({{ orphans|length }})</h2>
    <p class="muted">Cette taxonomie retire ces intentions ou en change le sens : ces fiches ne routeront plus par intention (L2) tant qu'une personne ne les revalide pas. Cochez « J'accepte » sous le tableau pour figer quand même.</p>
    <ul class="tight">{% for o in orphans %}<li>{{ o.name }}{% if o.intent %} — {{ o.intent }}{% endif %}</li>{% endfor %}</ul></div>{% endif %}
  {% if tax_source %}
  <form method="post" action="/review/{{ client }}/taxonomy" class="card" onsubmit="return confirm('Figer cette taxonomie relance la passe C sur chaque fiche routable au prochain « Reconstruire et mesurer » : environ {{ calls }} appels du modèle, {{ pass_c_minutes }}. Continuer ?')">
    <h2>{{ 'Proposition de la passe B' if tax_source == 'proposed' else 'Taxonomie figée' }} — à figer</h2>
    <p class="muted">Un identifiant : des mots français en MAJUSCULES séparés par « _ », l'action d'abord, jamais le nom d'une application (elle est un champ à part). Libellé court (120 caractères), description d'une phrase (200).</p>
    <input type="hidden" name="source" value="{{ tax_source }}"><input type="hidden" name="source_build" value="{{ source_build }}"><input type="hidden" name="base_version" value="{{ base_version }}"><input type="hidden" name="rows" value="{{ rows|length }}">
    <table><tr><th>Entrée</th><th>Identifiant</th><th>Libellé</th><th>Description</th><th>Membres</th><th>Supprimer</th></tr>
    {% for r in rows %}{% set i = loop.index0 %}<tr>
      <td class="muted">{{ r.n or '' }}</td>
      <td><input type="hidden" name="orig_{{ i }}" value="{{ r.orig }}"><input type="text" name="id_{{ i }}" value="{{ r.id }}" maxlength="120" autocomplete="off"></td>
      <td><input type="text" name="label_fr_{{ i }}" value="{{ r.label_fr }}" maxlength="120" autocomplete="off"></td>
      <td><input type="text" name="description_fr_{{ i }}" value="{{ r.description_fr }}" maxlength="200" autocomplete="off"></td>
      <td class="muted">{% if r.fiches %}{{ r.fiches }} fiche(s){% endif %}{% if r.members %}<br>{{ r.members|join(', ') }}{% endif %}{% if r.origin == 'unmapped' %}<br><span class="badge warn">non regroupée</span>{% endif %}
        {% for w in r.warnings %}<div class="badge warn">{{ w }}</div>{% endfor %}</td>
      <td><input type="checkbox" name="delete" value="{{ i }}" {{ 'checked' if r.delete }}></td></tr>{% endfor %}
    </table>
    {% if orphans %}<p><label><input type="checkbox" name="orphans_ok" value="1"> J'accepte : les fiches listées plus haut perdent leur intention validée et sont à revalider.</label></p>{% endif %}
    <div class="btns"><button class="b-main" name="decision" value="freeze">Figer cette taxonomie</button>
      {% if tax_source == 'proposed' %}<button class="b-soft" name="decision" value="as_is">Figer la proposition telle quelle</button>{% endif %}</div>
    <p class="muted">Figer relance la passe C sur chaque fiche routable au prochain « Reconstruire et mesurer » (environ {{ calls }} appels du modèle, {{ pass_c_minutes }}).</p>
  </form>
  {% else %}<div class="card muted">Pas encore de proposition : « Reconstruire et mesurer » lance la passe B tant qu'aucune taxonomie n'est figée.</div>{% endif %}
{% endif %}
</main>
</body>
</html>
"""
