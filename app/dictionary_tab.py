# =====================================================================
# KnowledgeEngine -- review of the client's software dictionary (V10 slice 5, pilier 2)
#
# The engine learns a client's dictionary from its KB, and counts the new product names live
# questions bring (fn-kecore, dictionary_service.py). Nothing enters the dictionary without a
# person: this tab shows the candidates seen in enough distinct questions, and the entries of the
# current dictionary, and records a decision (accept, accept as another spelling of an entry,
# reject; reject an entry). Decisions take effect at the next kecore run.
#
# Same rules as the labeling tab: claims-only access (tenant + group, then the client), same-origin
# POSTs, every value checked again by the engine (it validates the decision itself), messages as codes.
# =====================================================================
import re
from urllib.parse import urlparse

from flask import Blueprint, abort, redirect, render_template_string, request, url_for

MESSAGES = {
    "accepted": "Ajouté : pris en compte au prochain run kecore.",
    "rejected": "Rejeté : il ne sera plus proposé.",
    "error": "Le moteur a refusé la décision (déjà décidée, ou valeur invalide).",
    "unavailable": "Le moteur ne répond pas : réessayez dans un instant.",
}
_ENTRY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _same_origin() -> bool:
    src = request.headers.get("Origin") or request.headers.get("Referer") or ""
    return bool(src) and urlparse(src).netloc == request.host


def create_dictionary_blueprint(engine, deps: dict):
    """engine: dictionary(client) and decide(body) of app/kecore_client.EngineClient, or None.
    deps: allowed_clients(), display_name(), label_access()."""
    bp = Blueprint("dictionary", __name__)

    def _guard(client=None) -> list:
        if not deps["label_access"]():
            abort(403)
        clients = list(deps["allowed_clients"]())
        if not clients or (client is not None and client not in clients):
            abort(403)
        return clients

    @bp.route("/dictionary")
    def home():
        clients = _guard()
        client = request.args.get("client") or clients[0]
        if client not in clients:
            abort(403)
        data, error = None, None
        if engine is None:
            error = "Le moteur n'est pas relié à l'application (paramètres KECORE_FUNCTION_URL / KEY)."
        else:
            try:
                data = engine.dictionary(client)
            except Exception as exc:
                error = f"Lecture impossible : {type(exc).__name__}"
        return render_template_string(PAGE, clients=clients, client=client, d=data, error=error,
                                      msg=MESSAGES.get(request.args.get("msg", "")),
                                      display_name=deps["display_name"]())

    @bp.route("/dictionary/<client>/decide", methods=["POST"])
    def decide(client):
        _guard(client)
        if not _same_origin():
            abort(403)
        if engine is None:
            abort(503)
        choice = request.form.get("decision", "")
        body = {"client": client, "by": (deps["display_name"]() or "")[:120]}
        if choice in ("accept", "accept_as", "reject"):
            body.update(term=request.form.get("term", ""), accept=choice != "reject")
            canonical = request.form.get("canonical", "").strip()
            if choice == "accept_as":
                if not _ENTRY_RE.fullmatch(canonical):
                    abort(400)
                body["canonical"] = canonical
        elif choice == "reject_entry":
            body.update(entry=request.form.get("entry", ""), accept=False)
        else:
            abort(400)
        try:
            engine.decide(body)
            code = "rejected" if not body["accept"] else "accepted"
        except Exception as exc:
            code = "error" if "HTTP 4" in str(exc) else "unavailable"
        return redirect(url_for("dictionary.home", client=client, msg=code))

    return bp


PAGE = """
<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>KnowledgeEngine — Dictionnaire</title>
<style>
  :root{--bg:#f7f4ef;--panel:#fff;--panel-2:#faf3ea;--line:#e8ddd0;--txt:#20211f;--muted:#7a7267;--accent:#e2703a;
    --blue-tint:#e9f0fb;--ok:#1f9d76;--btn:#14172a}
  *{box-sizing:border-box}
  body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt);line-height:1.55}
  header{padding:14px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;background:var(--panel-2);flex-wrap:wrap}
  header h1{margin:0;font-size:1.02rem;font-weight:650}
  header nav{display:flex;gap:14px;flex-wrap:wrap}
  header nav a{color:var(--txt);text-decoration:none;font-size:.85rem}
  header nav a.on{font-weight:650;border-bottom:2px solid var(--accent)}
  .who{margin-left:auto;color:var(--muted);font-size:.8rem}
  main{max-width:1000px;margin:0 auto;padding:20px 16px 48px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
  .card h2{margin:0 0 10px;font-size:.98rem}
  table{width:100%;border-collapse:collapse}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:.86rem;vertical-align:top}
  th{color:var(--muted);font-size:.76rem;text-transform:uppercase;letter-spacing:.03em}
  .muted{color:var(--muted);font-size:.85rem}
  .msg{padding:10px 12px;border-radius:8px;background:var(--blue-tint);margin-bottom:14px;font-size:.86rem}
  .err{background:#fbe4e0;color:#b3372a}
  form.inline{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:0}
  button{font:inherit;font-weight:600;border:0;border-radius:8px;padding:6px 10px;cursor:pointer;font-size:.82rem}
  .b-ok{background:var(--ok);color:#fff}.b-main{background:var(--btn);color:#fff}.b-soft{background:var(--line);color:var(--txt)}
  select{font:inherit;font-size:.82rem;padding:5px;border:1px solid var(--line);border-radius:8px}
  .strike{text-decoration:line-through;color:var(--muted)}
</style></head><body>
<header><h1>KnowledgeEngine — Dictionnaire</h1>
  <nav><a href="/diag">Assistant</a><a href="/itsm">Tickets ITSM</a><a href="/labels">Labellisation</a><a class="on" href="/dictionary">Dictionnaire</a></nav>
  <span class="who">{{ display_name }}</span></header>
<main>
{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
{% if error %}<div class="msg err">{{ error }}</div>{% endif %}
{% if clients|length > 1 %}
<form method="get" action="/dictionary" class="card"><select name="client" onchange="this.form.submit()">{% for c in clients %}<option value="{{ c }}" {{ 'selected' if c == client }}>{{ c }}</option>{% endfor %}</select></form>
{% endif %}
{% if d %}
<div class="card">
  <h2>Nouveaux noms vus dans les questions ({{ d.ready|length }})</h2>
  <p class="muted">Un nom de logiciel cité après « l'application », « le logiciel »… dans au moins {{ d.min_observations }} questions différentes.
    {{ d.watching }} autre(s) en observation. Seul le nom est conservé, jamais la question.</p>
  <table><tr><th>Nom</th><th>Questions</th><th>Décision</th></tr>
  {% for r in d.ready %}<tr><td><b>{{ r.spelling }}</b></td><td>{{ r.seen }}</td><td>
    <form class="inline" method="post" action="/dictionary/{{ client }}/decide">
      <input type="hidden" name="term" value="{{ r.term }}">
      <button class="b-ok" name="decision" value="accept">Nouveau logiciel</button>
      <select name="canonical"><option value="">— autre nom de… —</option>{% for e in d.dictionary %}<option value="{{ e.id }}">{{ e.forms|join(' / ') }}</option>{% endfor %}</select>
      <button class="b-main" name="decision" value="accept_as">Autre nom</button>
      <button class="b-soft" name="decision" value="reject">Pas un logiciel</button>
    </form></td></tr>
  {% else %}<tr><td colspan="3" class="muted">Aucun nom à examiner.</td></tr>{% endfor %}</table>
</div>
<div class="card">
  <h2>Dictionnaire actuel ({{ d.dictionary|length }} entrées, carte {{ d.run_id or '—' }})</h2>
  <table><tr><th>Entrée</th><th>Écritures</th><th></th></tr>
  {% for e in d.dictionary %}<tr><td class="{{ 'strike' if e.rejected }}">{{ e.id }}</td><td class="{{ 'strike' if e.rejected }}">{{ e.forms|join(' · ') }}</td><td>
    {% if e.rejected %}<span class="muted">rejetée — retirée au prochain run</span>{% else %}
    <form class="inline" method="post" action="/dictionary/{{ client }}/decide"><input type="hidden" name="entry" value="{{ e.id }}">
      <button class="b-soft" name="decision" value="reject_entry">Ce n'est pas un logiciel</button></form>{% endif %}</td></tr>
  {% endfor %}</table>
  <p class="muted">Les décisions s'appliquent au prochain run kecore (POST /api/kecore/runs) : les fiches doivent être relues avec le nouveau dictionnaire.</p>
</div>
{% if d.decided %}<div class="card"><h2>Déjà décidés</h2><table>
  {% for r in d.decided %}<tr><td>{{ r.spelling }}</td><td>{{ 'ajouté' if r.status == 'accepted' else 'rejeté' }}{% if r.canonical %} (→ {{ r.canonical }}){% endif %}</td><td class="muted">{{ r.decided_by }}</td></tr>{% endfor %}
</table></div>{% endif %}
{% endif %}
</main></body></html>
"""
