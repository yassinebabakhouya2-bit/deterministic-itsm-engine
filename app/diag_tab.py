# =====================================================================
# KnowledgeEngine v9 -- Diagnostic tab (deterministic agentic RAG).
#
# Routes:
#   GET  /diag                     list of my sessions + new diagnostic form
#   POST /diag/new                 start a session (text and/or screenshot)
#   GET  /diag/s/<id>              fiche choice, step summary, current step, help
#   POST /diag/s/<id>/reply        action (done/blocked/...), text and/or screenshot
#   POST /diag/s/<id>/writeback    an ITSM agent validates the note for the session's ServiceNow ticket
#   POST /api/servicenow/webhook   ServiceNow ticket event (HMAC signed, JSON in / JSON out)
#   POST /diag/internal/sweep      no-op kept for compatibility (HMAC signed)
#
# The engine itself lives in orchestration/guide/ (pure FSM + ports). This
# module only does access control, form handling and rendering. When the Web App
# is linked to the deterministic engine (deps["engine"], app/kecore_client.py),
# fn-kecore finds the fiche first and its verified steps are shown word for word;
# the search index answers when the engine abstains or cannot be reached
# (orchestration/guide/kefind_ports.py).
#
# Security rules, enforced server-side:
#   - user routes: the client must be in the caller's allowed clients (Easy Auth
#     claims, same resolution as the assistant); a session is visible to its author
#     and, for ServiceNow sessions, to users holding ITSM access;
#   - POSTs from the browser must carry a same-origin Origin/Referer;
#   - the webhook/sweep are signed (HMAC-SHA256 over "<timestamp>.<body>", 5 minute
#     window) with DIAG_WEBHOOK_SECRET and are DISABLED (404) when it is unset; they
#     are the only routes excluded from Easy Auth (scripts/enable-diagnostic-webhook.ps1);
#   - ticket text, OCR text and model output are untrusted: Flask autoescapes
#     everything, nothing here is marked |safe;
#   - screenshots stay in memory for the request (never stored);
#   - write-back (V10 slice 6): this app never writes to ServiceNow and holds no
#     ServiceNow credential. An ITSM agent who can see the session validates the
#     note, built from the fiche and its steps only, never from what was typed
#     (orchestration/guide/writeback.py); the validated request is a row of the
#     diagwriteback table, executed by its own Logic App (itsm/writeback/, dryRun first);
#   - messages between pages travel as codes, never as text taken from the URL.
# =====================================================================
import hashlib
import hmac
import html as _html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml
from flask import Blueprint, abort, jsonify, redirect, render_template_string, request, url_for

from guide.contracts import ACTION_RE
from guide.fsm import Thresholds
from guide.kefind_ports import with_engine
from guide.ports import build_ports
from guide.service import Conflict, GuideService, NotFound, SESSION_ID_RE, TableStore
from guide.textutil import kb_text
from guide.writeback import KINDS, TICKET_RE, handover_action, request_row, statuses, work_note

TABLE = "diagsessions"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_MIMES = {"image/png", "image/jpeg", "image/webp"}
STATE_LABELS = {"LOCATE": "Recherche de la fiche", "GUIDING": "Résolution guidée",
                "SOLVED": "Résolu", "STUCK": "En attente d'une nouvelle description"}
# Messages between pages travel as codes: a crafted link cannot make this site show its own text.
MESSAGES = {
    "empty": "Collez un texte ou joignez une capture.",
    "start_failed": "Erreur technique au démarrage : réessayez dans un instant.",
    "conflict": "Session modifiée en parallèle, réessayez.",
    "ticket": "Numéro d'incident invalide : INC suivi de 7 à 10 chiffres.",
    "wb_saved": "Demande validée : l'exécuteur ServiceNow la traite sous 2 minutes (état ci-dessous).",
    "wb_exists": "Cette demande est déjà validée pour ce ticket (état ci-dessous).",
    "wb_no_action": "Aucune action du module ITSM ne correspond à cette fiche : ajoutez la note de travail.",
    "wb_invalid": "Rien à écrire : la session n'a pas de fiche, ou pas de numéro de ticket valide.",
    "wb_unavailable": "La file d'écriture ne répond pas : réessayez dans un instant.",
}
RETRYABLE = ("error", "not_found", "inactive")  # a failed write may be validated again, a written one never
STALE_RUNNING = timedelta(minutes=15)   # an executor run stopped mid-row: the row may be validated again
log = logging.getLogger(__name__)


def _load_writeback_config(path: Path) -> dict:
    """config/diag-writeback.yaml; a handover rule whose pattern does not compile is dropped."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        cfg = {}
    rules = []
    for rule in cfg.get("handover") or []:
        try:
            re.compile(str(rule.get("label_pattern") or ""))
        except (AttributeError, re.error):
            continue
        rules.append(rule)
    return {"table": str(cfg.get("table") or "diagwriteback"), "handover": rules,
            "clients": [str(c) for c in cfg.get("clients") or []]}


def _retryable(row: dict, now: datetime) -> bool:
    status = row.get("executionStatus") or ""
    if status in RETRYABLE:
        return True
    if status == "running":
        try:
            started = datetime.strptime(str(row.get("executionStartedUtc") or "")[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return True
        return now - started.replace(tzinfo=timezone.utc) > STALE_RUNNING
    return False


WRITEBACK_CONFIG = _load_writeback_config(Path(__file__).resolve().parent.parent / "config" / "diag-writeback.yaml")


def _same_origin():
    src = request.headers.get("Origin") or request.headers.get("Referer") or ""
    return bool(src) and urlparse(src).netloc == request.host


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str, now=None) -> bool:
    """HMAC-SHA256 over "<timestamp>.<body>", constant-time compare, 5 minute window."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - ts) > 300:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").removeprefix("sha256="))


class WritebackTable:
    """The diagwriteback table: one row per (session, kind). A first request is an atomic insert;
    a new validation after a failed write merges only into the row as it was read (If-Match)."""

    def __init__(self, table_client):
        self._client = table_client

    def get(self, partition_key, row_key):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self._client.get_entity(partition_key=partition_key, row_key=row_key)
        except ResourceNotFoundError:
            return None
        return {**dict(entity), "_etag": entity.metadata.get("etag")}

    def create(self, entity) -> bool:
        """True when inserted, False when the row already exists."""
        from azure.core.exceptions import ResourceExistsError

        try:
            self._client.create_entity(entity)
        except ResourceExistsError:
            return False
        return True

    def merge(self, entity, etag) -> bool:
        """False when the row changed (or vanished) since it was read."""
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
        from azure.data.tables import UpdateMode

        try:
            self._client.update_entity(entity, mode=UpdateMode.MERGE, etag=etag,
                                       match_condition=MatchConditions.IfNotModified)
        except (ResourceModifiedError, ResourceNotFoundError):
            return False
        return True


def create_diagnostic_blueprint(table_service, deps, store=None, writeback=None):
    """deps: allowed_clients(), user_id(), display_name(), itsm_access(), search_token(),
    aoai, load_engine_config, retrieve_hierarchy, fetch_document_chunks, and engine (optional:
    app/kecore_client.EngineClient; None = the search index alone).
    writeback: get/create/merge of the diagwriteback table (WritebackTable); None = from table_service."""
    bp = Blueprint("diag", __name__)
    bp.add_app_template_filter(kb_text, "kb_text")
    if store is None:
        try:
            table_service.create_table(TABLE)
        except Exception:
            pass                                        # exists, or not authorized yet
        store = TableStore(table_service.get_table_client(TABLE))
    if writeback is None and table_service is not None:
        writeback = WritebackTable(table_service.get_table_client(WRITEBACK_CONFIG["table"]))

    def ports_factory(client_id, images):
        cfg = deps["load_engine_config"](client_id)
        index = cfg["knowledge"]["index"]
        gen = cfg["generation"]
        headers = {"Authorization": f"Bearer {deps['search_token']()}"}
        th = Thresholds(**{k: v for k, v in (cfg.get("diagnostic") or {}).items()
                           if k in Thresholds.__dataclass_fields__})

        def search_docs(query):
            return deps["retrieve_hierarchy"](query, index, 5, 0, headers, client_id=client_id)[0]

        def fetch_chunks(parent_id):
            return deps["fetch_document_chunks"]([parent_id], index, headers, client_id=client_id).get(parent_id, [])

        index_ports = build_ports(aoai=deps["aoai"], model=gen["model"], seed=gen["seed"], search_docs=search_docs,
                                  fetch_chunks=fetch_chunks, images=images, thresholds=th)
        return with_engine(index_ports, deps.get("engine"), client_id)

    service = GuideService(store, ports_factory)

    # ------------------------------------------------------------ helpers
    def _clients():
        return deps["allowed_clients"]()

    def _can_see(view):
        if view["client_id"] not in _clients():
            return False
        return view.get("user_id") == deps["user_id"]() or (
            view.get("origin") == "servicenow" and deps["itsm_access"]())

    def _images():
        out = []
        for f in request.files.getlist("screenshot"):
            if not f or not f.filename:
                continue
            mime = (f.mimetype or "").lower()
            data = f.read(MAX_IMAGE_BYTES + 1)
            if mime not in ALLOWED_MIMES or len(data) > MAX_IMAGE_BYTES:
                abort(400, "Image refusée (PNG, JPEG ou WebP, 10 Mo maximum).")
            out.append((f.filename, data, mime))
        return out[:3]

    def _guard_post():
        if not _same_origin():
            abort(403)

    def _ticket_of(view) -> str:
        ticket = view.get("ticket_id") or ""
        return ticket if TICKET_RE.fullmatch(ticket) else ""

    def _writeback_on(client_id) -> bool:
        """Write-back is on for the clients of config/diag-writeback.yaml only (one executor per instance)."""
        return writeback is not None and client_id in WRITEBACK_CONFIG["clients"]

    def _writeback_panel(view):
        """What an ITSM agent may validate for the session's ticket; None when nothing can be written."""
        guide = view.get("guide") or {}
        if not _writeback_on(view["client_id"]) or not _ticket_of(view) or not guide.get("steps") \
                or not deps["itsm_access"]():
            return None
        rows = {}
        for kind in KINDS:
            try:
                row = writeback.get(view["client_id"], f"{view['session_id']}-{kind}")
            except Exception:
                row = None
            if row:
                rows[kind] = row
        labels, now = statuses(list(rows.values())), datetime.now(timezone.utc)
        # a row submitted but not yet picked up by the executor (polls every 2 minutes, runbook 19.3):
        # the page says so, rather than leaving the person to wonder why nothing happened yet
        pending = any(row.get("status") == "validated" and not row.get("executionStatus") for row in rows.values())
        return {"ticket": _ticket_of(view), "note": work_note(view, deps["display_name"](), now), "pending": pending,
                "action": handover_action(guide.get("title") or "", WRITEBACK_CONFIG["handover"]),
                "kinds": [{"kind": kind, "label": labels.get(kind), "can": kind not in rows or _retryable(rows[kind], now)}
                          for kind in KINDS]}

    def _submit(row) -> str:
        try:
            if writeback.create(row):
                return "wb_saved"
            existing = writeback.get(row["PartitionKey"], row["RowKey"])
        except Exception:
            log.exception("diagwriteback unavailable")
            return "wb_unavailable"
        if not existing or not _retryable(existing, datetime.now(timezone.utc)):
            return "wb_exists"
        retry = {**row, "executionStatus": "", "executionStartedUtc": "", "executedAtUtc": ""}
        try:
            return "wb_saved" if writeback.merge(retry, existing.get("_etag")) else "wb_exists"
        except Exception:
            log.exception("diagwriteback unavailable")
            return "wb_unavailable"

    # -------------------------------------------------------------- pages
    @bp.route("/diag")
    def home():
        clients = _clients()
        if not clients:
            abort(403)
        client_id = request.args.get("client_id") or clients[0]
        if client_id not in clients:
            abort(403)
        me, itsm = deps["user_id"](), deps["itsm_access"]()
        rows = []
        try:
            rows = [r for r in service.list_sessions(client_id)
                    if r.get("userId") == me or (r.get("origin") == "servicenow" and itsm)][:50]
        except Exception:
            pass
        return render_template_string(PAGE, view="home", clients=clients, client_id=client_id, rows=rows,
                                      states=STATE_LABELS, display_name=deps["display_name"](), itsm=itsm,
                                      error=MESSAGES.get(request.args.get("error", "")))

    @bp.route("/diag/new", methods=["POST"])
    def new():
        _guard_post()
        client_id = request.form.get("client_id", "")
        if client_id not in _clients():
            abort(403)
        text = (request.form.get("text") or "").strip()
        ticket = (request.form.get("ticket") or "").strip().upper()
        if ticket and not (deps["itsm_access"]() and TICKET_RE.fullmatch(ticket)):
            return redirect(url_for("diag.home", client_id=client_id, error="ticket"))
        images = _images()
        if not text and not images:
            return redirect(url_for("diag.home", client_id=client_id, error="empty"))
        try:
            view = service.start(client_id=client_id, user_id=deps["user_id"]() or "unknown", origin="app",
                                 text=text[:4000], images=images, ticket_id=ticket or None)
        except Exception:
            log.exception("diagnostic start failed")
            return redirect(url_for("diag.home", client_id=client_id, error="start_failed"))
        return redirect(url_for("diag.session", client_id=client_id, session_id=view["session_id"]))

    @bp.route("/diag/s/<session_id>")
    def session(session_id):
        client_id = request.args.get("client_id", "")
        if client_id not in _clients() or not SESSION_ID_RE.match(session_id):
            abort(404)
        try:
            view = service.get(client_id, session_id)
        except NotFound:
            abort(404)
        if not _can_see(view):
            abort(404)
        return render_template_string(PAGE, view="session", s=view, states=STATE_LABELS,
                                      display_name=deps["display_name"](), wb=_writeback_panel(view),
                                      error=MESSAGES.get(request.args.get("error", "")),
                                      msg=MESSAGES.get(request.args.get("msg", "")))

    @bp.route("/diag/s/<session_id>/reply", methods=["POST"])
    def reply(session_id):
        _guard_post()
        client_id = request.form.get("client_id", "")
        if client_id not in _clients() or not SESSION_ID_RE.match(session_id):
            abort(404)
        try:
            view = service.get(client_id, session_id)
        except NotFound:
            abort(404)
        if not _can_see(view):
            abort(404)
        text = (request.form.get("text") or "").strip()
        action = (request.form.get("action") or "").strip() or None
        if action is not None and not ACTION_RE.match(action):
            abort(400)
        images = _images()
        if action is None and not text and not images:
            return redirect(url_for("diag.session", client_id=client_id, session_id=session_id, _anchor="focus"))
        err = None
        for _ in range(2):                              # one retry on a concurrent writer
            try:
                service.reply(client_id=client_id, session_id=session_id, text=text[:4000],
                              images=images, action=action, event_id=request.form.get("event_id") or None)
                err = None
                break
            except Conflict:
                err = "conflict"
        return redirect(url_for("diag.session", client_id=client_id, session_id=session_id, error=err, _anchor="focus"))

    @bp.route("/diag/s/<session_id>/writeback", methods=["POST"])
    def writeback_request(session_id):
        _guard_post()
        client_id = request.form.get("client_id", "")
        if client_id not in _clients() or not SESSION_ID_RE.fullmatch(session_id):
            abort(404)
        if not _writeback_on(client_id) or not deps["itsm_access"]():
            abort(403)
        kind = request.form.get("kind", "")
        if kind not in KINDS:
            abort(400)
        try:
            view = service.get(client_id, session_id)
        except NotFound:
            abort(404)
        if not _can_see(view):
            abort(404)
        now = datetime.now(timezone.utc)
        action = None
        if kind == "handover":
            action = handover_action((view.get("guide") or {}).get("title") or "", WRITEBACK_CONFIG["handover"])
            if not action:
                return redirect(url_for("diag.session", client_id=client_id, session_id=session_id, msg="wb_no_action", _anchor="writeback"))
        try:
            row = request_row(view, kind, work_note(view, deps["display_name"](), now),
                              deps["user_id"]() or "unknown", deps["display_name"](), now, action)
        except ValueError:
            return redirect(url_for("diag.session", client_id=client_id, session_id=session_id, msg="wb_invalid", _anchor="writeback"))
        return redirect(url_for("diag.session", client_id=client_id, session_id=session_id, msg=_submit(row), _anchor="writeback"))

    # -------------------------------------------------- signed machine routes
    def _signed_body():
        secret = os.environ.get("DIAG_WEBHOOK_SECRET", "")
        if not secret:
            abort(404)                                  # feature disabled
        body = request.get_data(cache=True, as_text=False)
        if not verify_signature(secret, request.headers.get("X-KE-Timestamp", ""), body,
                                request.headers.get("X-KE-Signature", "")):
            abort(401)
        return body

    @bp.route("/api/servicenow/webhook", methods=["POST"])
    def webhook():
        body = _signed_body()
        client_id = os.environ.get("DIAG_WEBHOOK_CLIENT", "")
        if not client_id:
            abort(404)
        try:
            p = json.loads(body)
            number = str(p["ticket_number"])
            event_id = str(p["event_id"])
        except (ValueError, KeyError, TypeError):
            abort(400)
        sid = "sn-" + "".join(c for c in number if c.isalnum() or c in "-_")[:40]
        text = "\n".join(x for x in (p.get("short_description"), p.get("description"), p.get("comment")) if x)
        if not text.strip() or not SESSION_ID_RE.match(sid):
            abort(400)
        try:
            view = service.start(client_id=client_id, user_id="servicenow", origin="servicenow",
                                 text=text[:4000], ticket_id=number, session_id=sid, event_id=event_id)
        except Conflict:
            abort(409)
        return jsonify({k: view[k] for k in ("session_id", "state", "terminal", "guide", "current_step",
                                             "outbox")})

    @bp.route("/diag/internal/sweep", methods=["POST"])
    def sweep():
        _signed_body()
        client_id = os.environ.get("DIAG_WEBHOOK_CLIENT", "")
        return jsonify({"swept": service.sweep(client_id) if client_id else 0})

    bp.service = service
    return bp


PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Assistant — KnowledgeEngine</title>
<style>
:root{--bg:#faf7f2;--card:#fff;--txt:#2b2620;--mut:#7a6f62;--acc:#d9622b;--line:#e7dfd3;--ok:#2f7d4f;--warn:#b7791f;--bad:#b83b3b}
@media (prefers-color-scheme:dark){:root{--bg:#1b1815;--card:#252019;--txt:#efe8dc;--mut:#a79a8a;--line:#3a3329}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.55 system-ui,sans-serif}
header{display:flex;align-items:center;gap:18px;padding:14px 24px;border-bottom:1px solid var(--line)}
header h1{font-size:1.05rem;margin:0}header a{color:var(--txt);text-decoration:none;font-weight:600;font-size:.88rem}
header a.cur{border-bottom:2px solid var(--acc)}main{max-width:820px;margin:24px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:14px}
textarea{width:100%;min-height:80px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--txt);font:inherit}
select,input[type=file]{font:inherit;color:var(--txt)}
button{background:var(--acc);color:#fff;border:0;border-radius:9px;padding:9px 18px;font:inherit;font-weight:600;cursor:pointer}
button.alt{background:transparent;color:var(--txt);border:1px solid var(--line)}
button.ok{background:var(--ok)}button.lnk{background:none;color:var(--mut);padding:4px 8px;font-weight:400;font-size:.85rem;text-decoration:underline}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:.78rem;font-weight:700;background:var(--line)}
.b-SOLVED{background:#dff1e5;color:var(--ok)}.b-GUIDING{background:#fbeccc;color:var(--warn)}.b-STUCK{background:#f6dede;color:var(--bad)}
.mut{color:var(--mut);font-size:.85rem}.err{color:var(--bad);font-weight:600}
.warn{background:#fbeccc;color:#6b4a0a;border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:.9rem}
.bar{height:6px;background:var(--line);border-radius:99px;overflow:hidden;margin:10px 0 4px}.bar i{display:block;height:100%;background:var(--acc)}
ol.st{list-style:none;padding:0;margin:12px 0 0}ol.st li{display:flex;gap:10px;padding:7px 10px;border-radius:9px;align-items:flex-start}
ol.st li .n{flex:none;width:24px;height:24px;border-radius:50%;background:var(--line);display:flex;align-items:center;justify-content:center;font-size:.78rem;font-weight:700}
ol.st li.done{color:var(--mut)}ol.st li.done .n{background:var(--ok);color:#fff}
ol.st li.cur{background:rgba(217,98,43,.10);font-weight:600}ol.st li.cur .n{background:var(--acc);color:#fff}
.step h2{font-size:1.15rem;margin:2px 0 8px}.step .ins{font-size:1.02rem;white-space:pre-wrap}
.msg{border-left:3px solid var(--line);padding:6px 12px;margin:8px 0}.msg.u{border-color:var(--acc)}.msg.h{border-color:var(--ok);background:rgba(47,125,79,.06)}
.pick{display:block;width:100%;text-align:left;margin-top:10px;padding:12px 14px}
table{width:100%;border-collapse:collapse}td{padding:8px 4px;border-bottom:1px solid var(--line)}
a.l{color:var(--acc);text-decoration:none}
.b-verb{background:#e3eefb;color:#1f4f8a}
.info{background:#e3eefb;color:#1f3f6b;border-radius:10px;padding:10px 14px;margin-bottom:14px;font-size:.9rem}
.note{white-space:pre-wrap;font-size:.85rem;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;margin-top:8px}
input.tk{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--txt)}
</style></head><body>
<header><h1>KnowledgeEngine v9</h1>
<a class="cur" href="/diag">Assistant</a><a href="/itsm">Tickets ITSM</a>{% if labels_nav %}<a href="/labels">Labellisation</a><a href="/dictionary">Dictionnaire</a>{% endif %}
<a href="/classic" class="mut" style="font-weight:400">Assistant classique</a>
<span class="mut" style="margin-left:auto">{{ display_name }}</span></header>
<main>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
{% if msg %}<div class="info">{{ msg }}</div>{% endif %}

{% if view == "home" %}
<div class="card"><b>Décrivez votre problème</b>
<p class="mut">Collez le texte du problème et/ou joignez une capture d'écran. L'assistant trouve la fiche exacte, vous montre un résumé des étapes, puis vous accompagne étape par étape jusqu'à la résolution.</p>
<form method="post" action="/diag/new" enctype="multipart/form-data">
{% if clients|length > 1 %}<select name="client_id">{% for c in clients %}<option value="{{ c }}" {{ 'selected' if c==client_id }}>{{ c }}</option>{% endfor %}</select>
{% else %}<input type="hidden" name="client_id" value="{{ client_id }}">{% endif %}
<textarea name="text" style="min-height:110px" placeholder="Décrivez le problème, collez le message d'erreur…"></textarea>
{% if itsm %}<div class="row"><input class="tk" name="ticket" maxlength="14" placeholder="N° de ticket (optionnel)">
<span class="mut">INC… : la note de résolution pourra être ajoutée à l'incident, après votre validation.</span></div>{% endif %}
<div class="row"><input type="file" name="screenshot" accept="image/png,image/jpeg,image/webp" multiple>
<button type="submit">Trouver la solution</button></div></form></div>
<div class="card"><b>Conversations récentes</b>
{% if rows %}<table>{% for r in rows %}<tr>
<td><a class="l" href="/diag/s/{{ r.session_id }}?client_id={{ client_id }}">{{ r.title or r.session_id }}</a>
<div class="mut">{{ r.ticketId or ('Application') }} · {{ (r.updatedUtc or '')[:16].replace('T',' ') }}</div></td>
<td style="text-align:right"><span class="badge b-{{ r.state }}">{{ states.get(r.state, r.state) }}</span></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Aucune session.</p>{% endif %}</div>

{% else %}
{% set g = s.guide %}
<p><a class="l" href="/diag?client_id={{ s.client_id }}">← Conversations</a></p>
<div class="card" style="padding:12px 18px"><span class="badge b-{{ s.state }}">{{ states.get(s.state, s.state) }}</span>
<span class="mut">{% if s.ticket_id %} · ticket {{ s.ticket_id }}{% endif %}</span></div>

{% if s.risk_flags %}<div class="warn">⚠ Sujet sensible ({{ s.risk_flags|join(', ') }}) : suivez la fiche à la lettre et ne sautez aucune étape.</div>{% endif %}

{% if g %}
{% set n = g.steps|length %}{% set cur = s.current_step %}
<div class="card">
<div class="mut">📄 Fiche {{ 'la plus proche' if g.approximate else 'identifiée' }}{% if g.parent_id.startswith('kefind:') %} par le moteur déterministe{% elif g.origin == 'fallback' %} · résumé automatique{% endif %}</div>
<h2 style="margin:4px 0 6px;font-size:1.2rem">{{ g.title }}</h2>
{% if g.summary %}<div>{{ g.summary }}</div>{% endif %}
{% if g.approximate %}<div class="mut" style="margin-top:6px">Je n'ai pas pu confirmer que c'est exactement la bonne fiche : dites-le-moi si elle ne correspond pas.</div>{% endif %}
{% if g.preconditions %}<p class="mut" style="margin:10px 0 0"><b>Avant de commencer :</b> {{ g.preconditions|join(' · ') }}</p>{% endif %}
<div class="bar"><i style="width:{{ (100 * [cur, n]|min / n)|round|int }}%"></i></div>
<div class="mut">{{ [cur, n]|min }} / {{ n }} étapes faites</div>
<ol class="st">{% for st in g.steps %}<li class="{{ 'done' if loop.index0 < cur else ('cur' if loop.index0 == cur else '') }}">
<span class="n">{{ '✓' if loop.index0 < cur else loop.index }}</span><span>{{ st.title }}</span></li>{% endfor %}</ol>
{% if g.source_url %}<div class="mut" style="margin-top:8px">Source : <a class="l" href="{{ g.source_url }}" rel="noopener noreferrer">ouvrir la fiche</a></div>{% endif %}
</div>
{% endif %}

{% for m in s.messages %}
{% if m.role == 'user' and m.kind == 'text' %}<div class="msg u"><span class="mut">Vous{% if m.images %} · {{ m.images }} capture(s){% endif %}</span><br>{{ m.text }}</div>
{% elif m.kind == 'help' %}<div class="msg h"><span class="mut">Aide · étape {{ m.step }}</span><br><div style="white-space:pre-wrap">{{ m.text }}</div></div>
{% elif m.kind == 'notice' %}<div class="msg"><span class="mut">{{ m.text }}</span></div>
{% elif m.kind == 'done' %}{% endif %}{% endfor %}

{% if s.state == 'SOLVED' %}
<div class="card" style="border-left:4px solid var(--ok)"><b>✅ Problème résolu</b>
<p class="mut" style="margin:6px 0 0">Bravo. Vous pouvez démarrer une nouvelle recherche depuis la page des conversations.</p></div>

{% else %}
<form method="post" action="/diag/s/{{ s.session_id }}/reply" enctype="multipart/form-data">
<input type="hidden" name="client_id" value="{{ s.client_id }}">

{% if s.state == 'LOCATE' and s.choices %}
<div class="card" id="focus"><b>Quelle fiche correspond à votre problème ?</b>
<div class="mut">Choisissez-en une : je vous montre aussitôt les étapes.</div>
{% for c in s.choices %}<button class="alt pick" type="submit" name="action" value="pick:{{ loop.index }}">
<b>{{ c.title }}</b><br><span class="mut">correspondance {{ 'forte' if c.score >= 2.5 else ('moyenne' if c.score >= 1.5 else 'faible') }}</span></button>
{% if s.candidates and loop.index0 < s.candidates|length and s.candidates[loop.index0].excerpt %}
<details><summary class="mut" style="cursor:pointer;padding:4px 14px">▸ voir l'extrait</summary>
<div style="white-space:pre-wrap;padding:6px 14px" class="mut">{{ s.candidates[loop.index0].excerpt | kb_text(700) }}</div></details>{% endif %}{% endfor %}
<div class="row"><button class="lnk" type="submit" name="action" value="none">Aucune de ces fiches</button></div></div>
{% endif %}

{% if g and s.state == 'GUIDING' %}
{% if cur < n %}{% set stp = g.steps[cur] %}
<div class="card step" id="focus" style="border-left:4px solid var(--acc)"><div class="mut">Étape {{ cur + 1 }} sur {{ n }}</div>
<h2>{{ stp.title }}</h2><div class="ins">{{ stp.instruction }}</div>
{% if stp.verbatim_from_kb %}<div style="margin-top:6px"><span class="badge b-verb">Texte exact de la fiche</span></div>{% endif %}
<div class="row"><button class="ok" type="submit" name="action" value="done">✓ C'est fait</button>
<button class="alt" type="submit" name="action" value="blocked">✗ Ça ne marche pas</button>
<button class="alt" type="submit" name="action" value="explain">? Expliquer</button>
{% if cur > 0 %}<button class="alt" type="submit" name="action" value="back">← Précédente</button>{% endif %}</div>
{% if s.step_attempts >= 2 %}<div class="mut" style="margin-top:10px">Toujours bloqué ? <button class="lnk" type="submit" name="action" value="wrong_fiche">Essayer une autre fiche</button></div>{% endif %}
</div>
{% else %}
<div class="card" id="focus" style="border-left:4px solid var(--ok)"><b>Toutes les étapes sont faites. Le problème est-il résolu ?</b>
{% if g.verification %}<ul>{% for x in g.verification %}<li>{{ x }}</li>{% endfor %}</ul>{% endif %}
<div class="row"><button class="ok" type="submit" name="action" value="solved_yes">✓ Oui, résolu</button>
<button class="alt" type="submit" name="action" value="solved_no">✗ Non, toujours là</button>
<button class="alt" type="submit" name="action" value="back">← Revoir la dernière étape</button></div></div>
{% endif %}
<div class="row" style="margin-top:0"><button class="lnk" type="submit" name="action" value="wrong_fiche">Ce n'est pas la bonne fiche</button></div>
{% endif %}

<div class="card"{% if not g and not s.choices %} id="focus"{% endif %}><div class="mut">{% if g %}Une question ou un blocage sur cette étape ? Décrivez-le ou joignez une capture.{% elif s.state == 'STUCK' %}Décrivez le problème autrement ou joignez une capture.{% else %}Précisez le problème pour affiner la recherche (optionnel).{% endif %}</div>
<textarea name="text" placeholder="Votre message…"></textarea>
<div class="row"><input type="file" name="screenshot" accept="image/png,image/jpeg,image/webp" multiple>
<button type="submit">Envoyer</button></div></div>
</form>
{% endif %}
{% if wb %}
<div class="card" id="writeback"><b>Ticket {{ wb.ticket }} : note de résolution</b>
<div class="mut">Ajoutée au ticket en note de travail interne (jamais visible du demandeur) par l'exécuteur ServiceNow, après votre validation. Elle ne contient que la fiche et ses étapes, rien de ce qui a été tapé.
{% if wb.pending %} L'exécuteur ServiceNow passe toutes les 2 minutes : rafraîchissez la page après ce délai pour voir le résultat.{% endif %}</div>
<details><summary class="mut" style="cursor:pointer;margin-top:8px">▸ voir la note</summary><div class="note">{{ wb.note }}</div></details>
<form method="post" action="/diag/s/{{ s.session_id }}/writeback"><input type="hidden" name="client_id" value="{{ s.client_id }}">
<div class="row">{% for k in wb.kinds %}{% if k.can and (k.kind == 'work_note' or wb.action) %}
<button class="{{ 'ok' if k.kind == 'work_note' else 'alt' }}" type="submit" name="kind" value="{{ k.kind }}">{% if k.kind == 'work_note' %}{{ 'Valider à nouveau la note' if k.label else 'Valider : ajouter la note au ticket' }}{% else %}{{ 'Valider à nouveau le transfert' if k.label else 'Transférer au module ITSM' }} ({{ wb.action }}){% endif %}</button>
{% endif %}{% if k.label %}<span class="mut">{{ 'Note' if k.kind == 'work_note' else 'Transfert' }} : {{ k.label }}</span>{% endif %}{% endfor %}</div>
</form></div>
{% endif %}
{% endif %}
</main></body></html>"""
