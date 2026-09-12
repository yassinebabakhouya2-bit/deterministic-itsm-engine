# =====================================================================
# KnowledgeEngine v9 — Web interface (Jalon 4, auth updated Jalon 5)
#
# Thin Flask front end over orchestration/answer.py's
# answer_query_core_keyless(): retrieval + A3 hierarchy split + Structured
# Outputs generation, unchanged from Jalon 3 — this file adds nothing to
# the RAG pipeline itself, only a form and an HTTP entry point.
#
# Credentials: no admin keys, no secrets anywhere in this app. Auth is
# Azure AD RBAC via DefaultAzureCredential, which resolves to the App
# Service's system-assigned managed identity in Azure (local dev: an
# interactive `az login` session). Required roles, granted in
# infra/modules/roles.bicep:
#   - Search Index Data Reader   on the Search service
#   - Cognitive Services OpenAI User   on the Foundry account
#
# Client resolution (Jalon 5 — replaces the Jalon 4 ALLOWED_CLIENTS
# stopgap): the client(s) a given request may query are resolved from the
# X-MS-CLIENT-PRINCIPAL claims Easy Auth itself attaches to every
# authenticated request (tenant, then Entra group) — never from a value
# the browser posted. See app/auth.py and project memory
# jalon5-auth-isolation.md for the full design. Deny-by-default: an
# unrecognized tenant, or a tenant with no matching group, sees no client
# at all, not a fallback.
# =====================================================================
import os
import sys
from pathlib import Path

from azure.identity import DefaultAzureCredential
from flask import Flask, render_template_string, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestration"))
from answer import (  # noqa: E402 -- reuse orchestration logic (axiom A4), not a duplicate
    answer_query_core_keyless,
    build_aoai_client_keyless,
    load_engine_config,
)

from auth import build_access_maps, parse_client_principal, resolve_allowed_clients  # noqa: E402

# Built once at process start:
# - DefaultAzureCredential caches/refreshes tokens internally, not per-request.
# - The tenant/group -> client_id maps come from engine.<client>.yaml files,
#   which don't change without a redeploy -- no need to reload per-request.
_credential = DefaultAzureCredential()
_aoai_client = build_aoai_client_keyless(_credential)
_TENANT_ONLY_MAP, _TENANT_GROUP_MAP = build_access_maps()

# Local dev only: when there is NO X-MS-CLIENT-PRINCIPAL header at all (Easy
# Auth is not in front of this process, e.g. `python app.py` without an App
# Service), fall back to this comma-separated allowlist instead of denying
# everyone outright. This path is NEVER taken in Azure once Easy Auth's
# globalValidation.requireAuthentication is on: an unauthenticated request
# never reaches Flask, so the header is always present there (even if its
# claims resolve to zero clients, which is a real "access denied", not
# this fallback). Empty by default -- must be set explicitly to develop
# locally without Easy Auth.
_LOCAL_DEV_CLIENTS = [c.strip() for c in os.environ.get("LOCAL_DEV_CLIENTS", "").split(",") if c.strip()]

app = Flask(__name__)

PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KnowledgeEngine v9 — Démo</title>
<style>
  :root{ --bg:#0f172a; --panel:#1e293b; --line:#334155; --txt:#e2e8f0;
    --muted:#94a3b8; --accent:#38bdf8; --ok:#34d399; --warn:#fbbf24; }
  *{box-sizing:border-box}
  body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    background:var(--bg);color:var(--txt);line-height:1.5}
  header{padding:20px 24px;border-bottom:1px solid var(--line)}
  header h1{margin:0;font-size:1.15rem}
  header p{margin:4px 0 0;color:var(--muted);font-size:.85rem}
  main{max-width:720px;margin:0 auto;padding:24px}
  form{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px}
  label{display:block;font-size:.85rem;color:var(--muted);margin-bottom:4px}
  select,textarea{width:100%;background:#0b1220;color:var(--txt);border:1px solid var(--line);
    border-radius:6px;padding:8px;font-size:.95rem;font-family:inherit}
  textarea{min-height:80px;resize:vertical;margin-top:12px}
  button{margin-top:14px;background:var(--accent);color:#0b1220;border:none;border-radius:6px;
    padding:9px 18px;font-weight:600;cursor:pointer;font-size:.9rem}
  button:hover{opacity:.9}
  .error{margin-top:18px;padding:12px 14px;border:1px solid #7f1d1d;background:#2a1010;
    border-radius:8px;color:#fca5a5;font-size:.9rem}
  .denied{margin-top:18px;padding:16px;border:1px solid #7f1d1d;background:#2a1010;
    border-radius:10px;color:#fca5a5;font-size:.9rem}
  .answer{margin-top:18px;padding:16px;border:1px solid var(--line);background:var(--panel);
    border-radius:10px}
  .answer.ambiguous{border-color:var(--warn)}
  .sources{margin-top:12px;font-size:.85rem;color:var(--muted)}
  .src{padding:2px 0}
  .src.used{color:var(--ok)}
  .tag{display:inline-block;font-size:.7rem;padding:1px 6px;border-radius:4px;margin-left:6px}
  .tag.primary{background:#0c4a6e;color:#7dd3fc}
  .tag.annex{background:#312e81;color:#c7d2fe}
  footer{max-width:720px;margin:0 auto;padding:0 24px 24px;color:var(--muted);font-size:.75rem}
</style>
</head>
<body>
<header>
  <h1>KnowledgeEngine v9 — Assistant support IT</h1>
  <p>Connecté via Entra ID — le client affiché ci-dessous est déterminé automatiquement
     par votre organisation, pas choisi librement.</p>
</header>
<main>
  {% if not allowed_clients %}
  <div class="denied">
    Accès refusé : aucun client n'est associé à votre compte sur cette instance.
    Contactez votre administrateur si vous pensez que c'est une erreur.
  </div>
  {% else %}
  <form method="post">
    <label for="client_id">Client</label>
    <select name="client_id" id="client_id">
      {% for c in allowed_clients %}
      <option value="{{ c }}" {% if c == client_id %}selected{% endif %}>{{ c }}</option>
      {% endfor %}
    </select>
    <label for="query" style="margin-top:12px">Question</label>
    <textarea name="query" id="query" placeholder="Pose ta question...">{{ query }}</textarea>
    <button type="submit">Envoyer</button>
  </form>

  {% if error %}
  <div class="error">{{ error }}</div>
  {% endif %}

  {% if answer %}
  <div class="answer {% if answer.ambiguous %}ambiguous{% endif %}">
    <div>{{ answer.answer }}</div>
    {% if answer.ambiguous %}
    <div style="margin-top:8px;color:var(--warn);font-size:.85rem">
      ⚠ Réponse ambiguë{% if answer.unanswerable_reason %} — {{ answer.unanswerable_reason }}{% endif %}
    </div>
    {% endif %}
    <div class="sources">
      {% if answer.primary_source %}
      <div class="src {% if answer.primary_source.used %}used{% endif %}">
        {{ answer.primary_source.title }} <span class="tag primary">primaire</span>
        {% if answer.primary_source.used %}— utilisée{% endif %}
      </div>
      {% endif %}
      {% for s in answer.related_sources %}
      <div class="src {% if s.used %}used{% endif %}">
        {{ s.title }} <span class="tag annex">annexe</span>
        {% if s.used %}— utilisée{% endif %}
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}
  {% endif %}
</main>
<footer>Score reranker source primaire : {{ answer._trace.primary_reranker_score if answer else "" }}</footer>
</body>
</html>
"""


def get_search_bearer_token() -> str:
    return _credential.get_token("https://search.azure.com/.default").token


def _resolve_allowed_clients_for_request() -> list:
    """See app/auth.py module docstring for the full design. The
    X-MS-CLIENT-PRINCIPAL header is only absent when Easy Auth is not in
    front of this process at all (local dev) -- in Azure, with Easy Auth's
    globalValidation.requireAuthentication on, an unauthenticated request
    never reaches this code, so the header is always present there."""
    header_value = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    if header_value is None:
        return _LOCAL_DEV_CLIENTS
    claims = parse_client_principal(header_value)
    return resolve_allowed_clients(claims, _TENANT_ONLY_MAP, _TENANT_GROUP_MAP)


@app.route("/", methods=["GET", "POST"])
def index():
    answer = None
    error = None
    allowed_clients = _resolve_allowed_clients_for_request()
    client_id = request.form.get("client_id") or (allowed_clients[0] if allowed_clients else "")
    query = request.form.get("query", "")

    if request.method == "POST" and query.strip():
        if client_id not in allowed_clients:
            # Never trust the posted value alone -- re-validated here against
            # THIS request's own resolved set, not a global list.
            error = f"Client inconnu ou non autorisé pour votre compte : {client_id}"
        else:
            try:
                load_engine_config(client_id)  # fail fast with a clear error if misconfigured
                token = get_search_bearer_token()
                answer = answer_query_core_keyless(client_id, query, token, _aoai_client)
            except Exception as exc:  # surfaced to the user, not swallowed
                error = str(exc)

    return render_template_string(
        PAGE,
        allowed_clients=allowed_clients,
        client_id=client_id,
        query=query,
        answer=answer,
        error=error,
    )


@app.route("/healthz")
def healthz():
    # Deliberately does not list client ids (unauthenticated endpoint) --
    # just proves the app started and loaded its access maps.
    onboarded = len(_TENANT_ONLY_MAP) + len(_TENANT_GROUP_MAP)
    return {"status": "ok", "onboarded_clients": onboarded}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
