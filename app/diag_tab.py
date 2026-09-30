# =====================================================================
# KnowledgeEngine v9 -- Diagnostic tab (deterministic agentic RAG).
#
# Routes:
#   GET  /diag                     list of my sessions + new diagnostic form
#   POST /diag/new                 start a session (text and/or screenshot)
#   GET  /diag/s/<id>              session timeline, current question / plan / escalation
#   POST /diag/s/<id>/reply        answer the question (text, choice, screenshot)
#   POST /api/servicenow/webhook   ServiceNow ticket event (HMAC signed, JSON in / JSON out)
#   POST /diag/internal/sweep      escalate sessions whose reply deadline passed (HMAC signed)
#
# The engine itself lives in orchestration/diagnostic/ (pure FSM + ports). This
# module only does access control, form handling and rendering.
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
#   - screenshots stay in memory for the request (never stored).
# =====================================================================
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlparse

from flask import Blueprint, abort, jsonify, redirect, render_template_string, request, url_for

from diagnostic.contracts import FsmState
from diagnostic.fsm import Thresholds
from diagnostic.ports import build_ports
from diagnostic.service import Conflict, DiagnosticService, NotFound, SESSION_ID_RE, TableStore

TABLE = "diagsessions"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_MIMES = {"image/png", "image/jpeg", "image/webp"}
STATE_LABELS = {
    "INIT_TRIAGE": "Analyse", "NEED_DIAGNOSTIC_DATA": "En attente de votre réponse",
    "OCR_PROCESSING": "Lecture de la capture", "KB_MATCHED": "Fiche identifiée",
    "ACTION_PROPOSED": "Plan proposé", "HUMAN_ESCALATION": "Escalade humaine",
}
ESCALATION_LABELS = {
    "turn_budget": "Nombre maximal de questions atteint",
    "stagnation": "Les réponses n'apportent plus d'information nouvelle",
    "no_new_question": "Plus aucune question utile à poser",
    "ocr_unreadable": "Captures illisibles",
    "plan_invalid": "Aucun plan fiable n'a pu être produit à partir de la fiche",
    "deadline": "Délai dépassé", "reply_timeout": "Pas de réponse dans le délai",
    "iteration_guard": "Garde-fou technique",
}


def _label_reason(reason):
    if not reason:
        return ""
    if reason.startswith("risk:"):
        return "Sujet sensible (" + reason[5:] + ") : traitement humain obligatoire"
    if reason.startswith("technical_error"):
        return "Erreur technique : " + reason.split(":", 1)[-1]
    return ESCALATION_LABELS.get(reason, reason)


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


def create_diagnostic_blueprint(table_service, deps, store=None):
    """deps: allowed_clients(), user_id(), display_name(), itsm_access(), search_token(),
    aoai, load_engine_config, retrieve_hierarchy, fetch_document_chunks."""
    bp = Blueprint("diag", __name__)
    if store is None:
        try:
            table_service.create_table(TABLE)
        except Exception:
            pass                                        # exists, or not authorized yet
        store = TableStore(table_service.get_table_client(TABLE))

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

        return build_ports(aoai=deps["aoai"], model=gen["model"], seed=gen["seed"], search_docs=search_docs,
                           fetch_chunks=fetch_chunks, images=images, thresholds=th)

    service = DiagnosticService(store, ports_factory)

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
                                      states=STATE_LABELS, display_name=deps["display_name"](),
                                      error=request.args.get("error"))

    @bp.route("/diag/new", methods=["POST"])
    def new():
        _guard_post()
        client_id = request.form.get("client_id", "")
        if client_id not in _clients():
            abort(403)
        text = (request.form.get("text") or "").strip()
        images = _images()
        if not text and not images:
            return redirect(url_for("diag.home", client_id=client_id, error="Collez un texte ou joignez une capture."))
        try:
            view = service.start(client_id=client_id, user_id=deps["user_id"]() or "unknown", origin="app",
                                 text=text[:4000], images=images)
        except Exception as exc:
            return redirect(url_for("diag.home", client_id=client_id, error=f"Erreur : {type(exc).__name__}"))
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
                                      reason_label=_label_reason, display_name=deps["display_name"](),
                                      error=request.args.get("error"))

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
        images = _images()
        if not text and not images:
            return redirect(url_for("diag.session", client_id=client_id, session_id=session_id))
        err = None
        for _ in range(2):                              # one retry on a concurrent writer
            try:
                service.reply(client_id=client_id, session_id=session_id, text=text[:4000],
                              images=images, event_id=request.form.get("event_id") or None)
                err = None
                break
            except Conflict:
                err = "Session modifiée en parallèle, réessayez."
        return redirect(url_for("diag.session", client_id=client_id, session_id=session_id, error=err))

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
        return jsonify({k: view[k] for k in ("session_id", "state", "terminal", "turn", "confidence",
                                             "escalation_reason", "outbox", "plan")})

    @bp.route("/diag/internal/sweep", methods=["POST"])
    def sweep():
        _signed_body()
        client_id = os.environ.get("DIAG_WEBHOOK_CLIENT", "")
        return jsonify({"escalated": service.sweep(client_id) if client_id else 0})

    bp.service = service
    return bp


PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Assistant — KnowledgeEngine</title>
<style>
:root{--bg:#faf7f2;--card:#fff;--txt:#2b2620;--mut:#7a6f62;--acc:#d9622b;--line:#e7dfd3;--ok:#2f7d4f;--warn:#b7791f;--bad:#b83b3b}
@media (prefers-color-scheme:dark){:root{--bg:#1b1815;--card:#252019;--txt:#efe8dc;--mut:#a79a8a;--line:#3a3329}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 system-ui,sans-serif}
header{display:flex;align-items:center;gap:18px;padding:14px 24px;border-bottom:1px solid var(--line)}
header h1{font-size:1.05rem;margin:0}header a{color:var(--txt);text-decoration:none;font-weight:600;font-size:.88rem}
header a.cur{border-bottom:2px solid var(--acc)}main{max-width:860px;margin:24px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:14px}
textarea{width:100%;min-height:110px;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--txt);font:inherit}
select,input[type=file]{font:inherit;color:var(--txt)}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:8px 16px;font:inherit;font-weight:600;cursor:pointer}
button.alt{background:transparent;color:var(--txt);border:1px solid var(--line)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:.78rem;font-weight:700;background:var(--line)}
.b-ACTION_PROPOSED{background:#dff1e5;color:var(--ok)}.b-HUMAN_ESCALATION{background:#f6dede;color:var(--bad)}
.b-NEED_DIAGNOSTIC_DATA{background:#fbeccc;color:var(--warn)}
.mut{color:var(--mut);font-size:.85rem}.err{color:var(--bad);font-weight:600}
.msg-user{border-left:3px solid var(--acc)}.msg-asst{border-left:3px solid var(--line)}
ol.steps li{margin:6px 0}table{width:100%;border-collapse:collapse}td{padding:8px 4px;border-bottom:1px solid var(--line)}
a.l{color:var(--acc);text-decoration:none}
</style></head><body>
<header><h1>KnowledgeEngine v9</h1>
<a class="cur" href="/diag">Assistant</a><a href="/itsm">Tickets ITSM</a>
<a href="/classic" class="mut" style="font-weight:400">Assistant classique</a>
<span class="mut" style="margin-left:auto">{{ display_name }}</span></header>
<main>
{% if error %}<p class="err">{{ error }}</p>{% endif %}

{% if view == "home" %}
<div class="card"><b>Décrivez votre problème</b>
<p class="mut">Collez le texte du problème et/ou joignez une capture d'écran. Vous voyez tout de suite la fiche de la base de connaissances la plus proche, puis l'assistant pose au plus 4 questions ciblées pour confirmer la bonne procédure, ou transmet à un humain avec tout ce qui a été appris.</p>
<form method="post" action="/diag/new" enctype="multipart/form-data">
{% if clients|length > 1 %}<select name="client_id">{% for c in clients %}<option value="{{ c }}" {{ 'selected' if c==client_id }}>{{ c }}</option>{% endfor %}</select>
{% else %}<input type="hidden" name="client_id" value="{{ client_id }}">{% endif %}
<textarea name="text" placeholder="Décrivez le problème, collez le message d'erreur…"></textarea>
<div class="row"><input type="file" name="screenshot" accept="image/png,image/jpeg,image/webp" multiple>
<button type="submit">Rechercher</button></div></form></div>
<div class="card"><b>Conversations récentes</b>
{% if rows %}<table>{% for r in rows %}<tr>
<td><a class="l" href="/diag/s/{{ r.session_id }}?client_id={{ client_id }}">{{ r.title or r.session_id }}</a>
<div class="mut">{{ r.ticketId or ('Application') }} · {{ (r.updatedUtc or '')[:16].replace('T',' ') }}</div></td>
<td style="text-align:right"><span class="badge b-{{ r.state }}">{{ states.get(r.state, r.state) }}</span></td></tr>{% endfor %}</table>
{% else %}<p class="mut">Aucune session.</p>{% endif %}</div>

{% else %}
<p><a class="l" href="/diag?client_id={{ s.client_id }}">← Conversations</a></p>
<div class="card"><span class="badge b-{{ s.state }}">{{ states.get(s.state, s.state) }}</span>
<span class="mut"> · tour {{ s.turn }}/4 · confiance {{ (s.confidence * 100)|round|int }} %{% if s.ticket_id %} · ticket {{ s.ticket_id }}{% endif %}</span></div>

{% if s.candidates and s.state != 'ACTION_PROPOSED' %}
{% set c0 = s.candidates[0] %}
<div class="card" style="border-left:3px solid var(--acc)">
<div class="mut">📄 Fiche la plus proche — à confirmer par le diagnostic
 · correspondance {{ 'forte' if c0.reranker_score >= 2.5 else ('moyenne' if c0.reranker_score >= 1.5 else 'faible') }}</div>
<b>{{ c0.title }}</b>
{% if c0.excerpt %}<details style="margin-top:6px"><summary class="mut" style="cursor:pointer">▸ voir l'extrait</summary>
<div style="white-space:pre-wrap;margin-top:6px">{{ c0.excerpt }}</div></details>{% endif %}
{% if s.candidates|length > 1 %}<div class="mut" style="margin-top:8px">Autres pistes : {% for c in s.candidates[1:] %}{{ c.title }}{{ ' · ' if not loop.last }}{% endfor %}</div>{% endif %}
</div>
{% endif %}

{% for m in s.messages %}
{% if m.role == 'user' %}<div class="card msg-user"><div class="mut">Vous{% if m.images %} · {{ m.images }} capture(s){% endif %}</div>{{ m.text }}</div>
{% elif m.kind == 'question' %}<div class="card msg-asst"><div class="mut">Question</div>{{ m.prompt.text_fr }}
<div class="mut">{{ m.prompt.why_needed }}</div></div>
{% elif m.kind == 'plan' %}<div class="card msg-asst"><div class="mut">Procédure — {{ m.plan.kb_title }}</div>
{% if m.plan.preconditions %}<p><b>Prérequis</b></p><ul>{% for x in m.plan.preconditions %}<li>{{ x }}</li>{% endfor %}</ul>{% endif %}
<ol class="steps">{% for st in m.plan.steps %}<li>{{ st.instruction }}</li>{% endfor %}</ol>
{% if m.plan.verification %}<p><b>Pour vérifier</b></p><ul>{% for x in m.plan.verification %}<li>{{ x }}</li>{% endfor %}</ul>{% endif %}
<div class="mut">Source : {{ m.plan.kb_title }}{% if m.plan.source_url %} · <a class="l" href="{{ m.plan.source_url }}" rel="noopener noreferrer">ouvrir</a>{% endif %}</div></div>
{% elif m.kind == 'escalation' %}<div class="card msg-asst"><div class="mut">Transmis à un humain</div>
<b>{{ reason_label(m.reason) }}</b>
<p class="mut">Vous pouvez aussi poser la question dans l'<a class="l" href="/classic">assistant classique</a>.</p>
{% if m.dossier and m.dossier.variables %}<p class="mut">Éléments recueillis : {% for v in m.dossier.variables %}{{ v.name }} = {{ v.value }}{{ ', ' if not loop.last }}{% endfor %}</p>{% endif %}</div>
{% endif %}{% endfor %}

{% if not s.terminal %}
{% set q = (s.messages | selectattr('kind','equalto','question') | list | last) %}
<div class="card"><form method="post" action="/diag/s/{{ s.session_id }}/reply" enctype="multipart/form-data">
<input type="hidden" name="client_id" value="{{ s.client_id }}">
{% if q and q.prompt.options %}<div class="row">{% for o in q.prompt.options %}
<button class="alt" type="submit" name="text" value="{{ o.label }}">{{ o.label }}</button>{% endfor %}</div>
<p class="mut">ou répondez librement :</p>{% endif %}
<textarea name="text" placeholder="Votre réponse…"></textarea>
<div class="row"><input type="file" name="screenshot" accept="image/png,image/jpeg,image/webp" multiple>
<button type="submit">Envoyer</button></div></form></div>
{% endif %}
{% endif %}
</main></body></html>"""
