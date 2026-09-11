# =====================================================================
# KnowledgeEngine v9 — Web interface (Jalon 4)
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
# Client resolution (stopgap before Jalon 5 — no real per-user auth yet):
# ALLOWED_CLIENTS env var (comma-separated client ids) is the only gate.
# Anyone who can reach this app's URL can query any client in that list —
# see project memory jalon4-app-interface.md for the discussion and why
# clienta/b/c and client-v/s are currently treated the same way (all live
# in Yassine's own sandbox tenant; there is no separate DXC-tenant
# boundary to enforce yet). Jalon 5 replaces this env var with a real
# Entra ID group -> clientId mapping.
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

ALLOWED_CLIENTS = [
    c.strip()
    for c in os.environ.get(
        "ALLOWED_CLIENTS", "clienta,clientb,clientc,client-v,client-s"
    ).split(",")
    if c.strip()
]

# Built once at process start (DefaultAzureCredential caches/refreshes tokens
# internally) -- not per-request.
_credential = DefaultAzureCredential()
_aoai_client = build_aoai_client_keyless(_credential)

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
  <p>Jalon 4 — démo. Pas encore d'authentification par utilisateur (Jalon 5) :
     le client est choisi manuellement ci-dessous.</p>
</header>
<main>
  <form method="post">
    <label for="client_id">Client</label>
    <select name="client_id" id="client_id">
      {% for c in clients %}
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
</main>
<footer>Score reranker source primaire : {{ answer._trace.primary_reranker_score if answer else "" }}</footer>
</body>
</html>
"""


def get_search_bearer_token() -> str:
    return _credential.get_token("https://search.azure.com/.default").token


@app.route("/", methods=["GET", "POST"])
def index():
    answer = None
    error = None
    client_id = request.form.get("client_id") or (ALLOWED_CLIENTS[0] if ALLOWED_CLIENTS else "")
    query = request.form.get("query", "")

    if request.method == "POST" and query.strip():
        if client_id not in ALLOWED_CLIENTS:
            error = f"Client inconnu ou non autorisé sur cette instance : {client_id}"
        else:
            try:
                load_engine_config(client_id)  # fail fast with a clear error if misconfigured
                token = get_search_bearer_token()
                answer = answer_query_core_keyless(client_id, query, token, _aoai_client)
            except Exception as exc:  # surfaced to the user, not swallowed
                error = str(exc)

    return render_template_string(
        PAGE, clients=ALLOWED_CLIENTS, client_id=client_id, query=query, answer=answer, error=error
    )


@app.route("/healthz")
def healthz():
    return {"status": "ok", "allowed_clients": ALLOWED_CLIENTS}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
