# =====================================================================
# KnowledgeEngine v9 -- ITSM action module, agent review tab (Jalon 10, step 10.3)
#
# Shows the ServiceNow tickets collected by itsm/poll (Logic App) and the
# proposals written by itsm/propose (Logic App: Graph facts -> GPT-4o with a
# closed action enum -> deterministic guards), and lets a human agent
# VALIDATE or REJECT each proposal. This step only RECORDS the decision in
# the itsmtickets row (reviewStatus / reviewedBy / approvedParamsJson ...).
# Nothing is executed on Entra ID or ServiceNow from the web app, ever: the
# executor (step 10.4) is a separate Logic App with its own identity that
# picks up validated rows. The Web App's managed identity keeps exactly the
# rights it already had (Storage Table Data Contributor on the account) --
# no Graph write permission is ever given to it.
#
# Security rules enforced SERVER-SIDE (never trusted from the form):
#   - access: tenant + Entra group from config/itsm.yaml, from Easy Auth's
#     validated claims only (auth.has_tenant_and_group), deny-by-default;
#   - only a row whose proposalStatus is 'pending_review' and that has no
#     reviewStatus yet can be validated; 'refused' rows (security guards)
#     can never be validated from the UI, only closed as handled manually;
#   - edited parameters are re-validated (group in allowlist, offboarding
#     steps subset of the proposed ones -- an agent may REMOVE steps, never
#     add new ones);
#   - concurrency: the form carries the proposal version (proposedAtUtc); the
#     update is conditional (If-Match on the ETag read in the same request,
#     retried if a poller MERGE lands in between) -> two agents cannot both decide;
#   - CSRF: POSTs must carry an Origin/Referer of this same host (the app
#     has no session store; Easy Auth's cookie would otherwise be sent on a
#     cross-site form post).
# Ticket text is untrusted (written by end users): Flask's
# render_template_string autoescapes everything, and nothing here is
# marked |safe.
# =====================================================================
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import yaml
from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode
import requests
from flask import Blueprint, abort, make_response, redirect, render_template_string, request, url_for

from auth import has_tenant_and_group, parse_client_principal, resolve_display_name, resolve_user_id

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "itsm.yaml"
with open(_CONFIG_PATH, encoding="utf-8") as _f:
    ITSM_CONFIG = yaml.safe_load(_f)

_PARTITION = ITSM_CONFIG["clientCode"]
_ALLOWED_GROUPS = list(ITSM_CONFIG.get("allowedGroups") or [])
_SN_INSTANCE = ITSM_CONFIG.get("serviceNowInstance", "")
_LOCAL_DEV = os.environ.get("LOCAL_DEV_ITSM") == "1"
_DELIVERY_VAULT = ITSM_CONFIG.get("deliveryKeyVault", "")

ACTION_LABELS = {
    "mfa_reset": "Réinitialisation MFA",
    "password_reset": "Réinitialisation du mot de passe",
    "group_add": "Ajout à un groupe",
    "license_assign": "Attribution de licence",
    "offboarding": "Départ collaborateur",
    "escalate": "Escalade",
}
STATUS_LABELS = {
    "pending_review": "À valider",
    "needs_human": "Traitement humain",
    "refused": "Refusé (garde-fou)",
}
REVIEW_LABELS = {
    "validated": "Validé — en attente d'exécution",
    "rejected": "Rejeté par l'agent",
    "handled_manually": "Traité manuellement",
}
REASON_LABELS = {
    "target_privileged_role": "Compte privilégié (rôle d'administration Entra)",
    "target_is_not_requester": "Le compte visé n'est pas celui du demandeur",
    "secret_requested_for_third_party": "Secret demandé pour un tiers",
    "requester_not_manager": "Demandeur différent du manager Entra",
    "group_not_allowlisted": "Groupe hors liste autorisée",
    "already_member": "Déjà membre du groupe",
    "no_free_license_seat": "Aucune licence libre dans le tenant",
    "llm_escalated": "Escaladé par le modèle",
    "low_confidence": "Confiance faible (< 0,6)",
    "subject_not_found_in_entra": "Utilisateur introuvable dans Entra ID",
}
EXECUTION_LABELS = {
    "running": "Exécution en cours",
    "success": "Exécuté",
    "partial": "Exécuté partiellement",
    "blocked": "Bloqué à l'exécution",
    "dry_run": "Simulation (dry run)",
    "error": "Erreur d'exécution",
}
OFFBOARDING_STEP_LABELS = {
    "disable_account": "Désactiver le compte",
    "revoke_sessions": "Révoquer les sessions",
    "remove_groups": "Retirer les groupes",
    "remove_licenses": "Retirer les licences",
}
QUEUES = [
    ("pending_review", "À valider"),
    ("needs_human", "Traitement humain"),
    ("refused", "Refusés"),
    ("reviewed", "Décidés"),
    ("new", "En analyse"),
]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _claims():
    header = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    return None if header is None else parse_client_principal(header)


def itsm_access_for_request() -> bool:
    claims = _claims()
    if claims is None:
        return _LOCAL_DEV
    access = ITSM_CONFIG.get("access") or {}
    return has_tenant_and_group(claims, access.get("entraTenantId"), access.get("entraGroup"))


def _reviewer():
    claims = _claims()
    if claims is None:
        return "local-dev", "Démo locale"
    return (resolve_user_id(claims) or "unknown-user"), (resolve_display_name(claims) or "Utilisateur")


def _same_origin() -> bool:
    src = request.headers.get("Origin") or request.headers.get("Referer") or ""
    return bool(src) and urlparse(src).netloc == request.host


def _queue_of(row) -> str:
    if row.get("reviewStatus"):
        return "reviewed"
    return row.get("proposalStatus") or "new"


def _proposal(row) -> dict:
    try:
        return json.loads(row.get("proposalJson") or "{}")
    except ValueError:
        return {}


def _exec_log(row) -> list:
    try:
        return json.loads(row.get("executionLog") or "[]")
    except ValueError:
        return []


def _split(value) -> list:
    return [v for v in (value or "").split(",") if v]


def create_itsm_blueprint(table_service, credential=None):
    bp = Blueprint("itsm", __name__)
    table = table_service.get_table_client(ITSM_CONFIG.get("table", "itsmtickets"))

    def _guard():
        if not itsm_access_for_request():
            abort(403)

    @bp.app_template_filter("reason_label")
    def _reason_label(code):
        return REASON_LABELS.get(code, code)

    @bp.route("/itsm")
    def queue():
        _guard()
        active = request.args.get("q", "pending_review")
        error = None
        rows = []
        try:
            rows = list(table.query_entities(
                "PartitionKey eq @pk", parameters={"pk": _PARTITION}))
        except Exception as exc:  # role not propagated, table missing...
            error = f"Lecture de la file impossible : {type(exc).__name__}"
        counts = {k: 0 for k, _ in QUEUES}
        for r in rows:
            counts[_queue_of(r)] = counts.get(_queue_of(r), 0) + 1
        shown = sorted([r for r in rows if _queue_of(r) == active], key=lambda r: r["RowKey"])
        return render_template_string(
            ITSM_PAGE, view="queue", queues=QUEUES, counts=counts, active=active, rows=shown,
            error=error, action_labels=ACTION_LABELS, status_labels=STATUS_LABELS,
            review_labels=REVIEW_LABELS, exec_labels=EXECUTION_LABELS, split=_split, display_name=_reviewer()[1])

    @bp.route("/itsm/t/<number>")
    def ticket(number):
        _guard()
        try:
            row = table.get_entity(partition_key=_PARTITION, row_key=number)
        except ResourceNotFoundError:
            abort(404)
        p = _proposal(row)
        return render_template_string(
            ITSM_PAGE, view="ticket", row=row, p=p, etag=row.metadata.get("etag", ""),
            action_labels=ACTION_LABELS, status_labels=STATUS_LABELS, review_labels=REVIEW_LABELS,
            step_labels=OFFBOARDING_STEP_LABELS, allowed_groups=_ALLOWED_GROUPS, split=_split,
            sn_instance=_SN_INSTANCE, msg=request.args.get("msg"), display_name=_reviewer()[1],
            approved=json.loads(row.get("approvedParamsJson") or "{}") if row.get("approvedParamsJson") else None,
            exec_labels=EXECUTION_LABELS, exec_log=_exec_log(row), me=_reviewer()[0])

    @bp.route("/itsm/t/<number>/decision", methods=["POST"])
    def decision(number):
        _guard()
        if not _same_origin():
            abort(403)
        proposed_at = request.form.get("proposed_at", "")
        choice = request.form.get("decision", "")
        comment = (request.form.get("comment") or "").strip()[:2000]

        def back(msg):
            return redirect(url_for("itsm.ticket", number=number, msg=msg))

        # Concurrency (fixed 2026-09-25): the raw row ETag cannot be carried by the
        # form -- the poller (itsm/poll) MERGEs every row every 5 min (lastSeenUtc),
        # so any page left open a few minutes always looked "modified". What must not
        # change under the agent's eyes is the PROPOSAL (proposedAtUtc) and the
        # absence of a decision; the If-Match below uses the ETag read in THIS
        # request, so two concurrent decisions still cannot both succeed.
        for _attempt in range(3):
            try:
                row = table.get_entity(partition_key=_PARTITION, row_key=number)
            except ResourceNotFoundError:
                abort(404)
            if row.get("reviewStatus"):
                return back("Ce ticket a déjà été décidé.")
            if proposed_at != (row.get("proposedAtUtc") or ""):
                return back("La proposition a changé entre-temps : vérifiez puis recommencez.")
            status = row.get("proposalStatus")
            p = _proposal(row)
            action = row.get("proposalAction") or p.get("action")

            patch = {"PartitionKey": _PARTITION, "RowKey": number}
            if choice == "validate":
                if status != "pending_review":
                    return back("Seule une proposition « À valider » peut être validée.")
                params = {"action": action}
                if action == "group_add":
                    group = request.form.get("group_name", "")
                    if group not in _ALLOWED_GROUPS:
                        return back("Groupe hors liste autorisée.")
                    params["group_name"] = group
                elif action == "offboarding":
                    proposed = set(p.get("offboarding_steps") or [])
                    steps = [st for st in request.form.getlist("steps") if st in proposed]
                    if not steps:
                        return back("Au moins une étape du départ doit rester cochée.")
                    params["offboarding_steps"] = steps
                elif action == "license_assign":
                    params["license_sku_hint"] = p.get("license_sku_hint", "")
                patch.update({"reviewStatus": "validated", "approvedParamsJson": json.dumps(params, ensure_ascii=False)})
            elif choice == "reject":
                if not comment:
                    return back("Un commentaire est obligatoire pour rejeter.")
                patch["reviewStatus"] = "rejected"
            elif choice == "manual":
                if status not in ("needs_human", "refused"):
                    return back("Action non disponible pour ce ticket.")
                patch["reviewStatus"] = "handled_manually"
            else:
                abort(400)

            reviewer_id, reviewer_name = _reviewer()
            patch.update({"reviewedById": reviewer_id, "reviewedByName": reviewer_name,
                          "reviewedAtUtc": _now_utc(), "reviewComment": comment})
            try:
                table.update_entity(patch, mode=UpdateMode.MERGE, etag=row.metadata.get("etag"),
                                    match_condition=MatchConditions.IfNotModified)
                break
            except ResourceModifiedError:
                continue  # a poller MERGE landed in between: re-read and re-check
        else:
            return back("Le ticket est en cours de mise à jour : réessayez dans quelques secondes.")
        return back("Décision enregistrée.")

    # ------------------------------------------------------------------ 10.4b one-time secret reveal
    # The temporary password / TAP created by the executor lives ONLY in the dedicated delivery vault.
    # It is shown once, to the agent who validated the ticket, then deleted. The row is claimed first
    # (If-Match) so two clicks / two tabs can never both reveal it. Never logged, never stored here.
    @bp.route("/itsm/t/<number>/reveal", methods=["POST"])
    def reveal(number):
        _guard()
        if not _same_origin():
            abort(403)
        try:
            row = table.get_entity(partition_key=_PARTITION, row_key=number)
        except ResourceNotFoundError:
            abort(404)

        def back(msg):
            return redirect(url_for("itsm.ticket", number=number, msg=msg))

        secret_ref = row.get("secretRef") or ""
        if not secret_ref or row.get("secretRevealedAtUtc"):
            return back("Aucun code à afficher (déjà remis ou inexistant).")
        reviewer_id, reviewer_name = _reviewer()
        if reviewer_id != row.get("reviewedById"):
            return back("Seul l'agent qui a validé ce ticket peut afficher le code.")
        if credential is None or not _DELIVERY_VAULT:
            return back("Coffre de remise non configuré.")
        # Order fixed 2026-09-25 (first live reveal burnt the row without showing anything):
        # 1) read the secret, 2) claim the row (If-Match), 3) delete + show. A failure before the
        # claim leaves everything retryable; two concurrent clicks -> only the claim winner shows it.
        url = f"https://{_DELIVERY_VAULT}.vault.azure.net/secrets/{secret_ref}?api-version=7.4"
        try:
            headers = {"Authorization": f"Bearer {credential.get_token('https://vault.azure.net/.default').token}"}
            resp = requests.get(url, headers=headers, timeout=15)
        except Exception as exc:
            return back(f"Coffre de remise inaccessible ({type(exc).__name__}) : réessayez.")
        if resp.status_code == 404:
            return back("Code introuvable ou expiré (validité 1 h) : relancez l'action si nécessaire.")
        if resp.status_code != 200:
            return back(f"Lecture du coffre refusée (HTTP {resp.status_code}) : droits de l'application sur {_DELIVERY_VAULT} à vérifier, puis réessayez.")
        value = resp.json().get("value", "")
        try:
            table.update_entity({"PartitionKey": _PARTITION, "RowKey": number,
                                 "secretRevealedAtUtc": _now_utc(), "secretRevealedByName": reviewer_name},
                                mode=UpdateMode.MERGE, etag=row.metadata.get("etag"),
                                match_condition=MatchConditions.IfNotModified)
        except ResourceModifiedError:
            return back("Le ticket a été modifié entre-temps : réessayez.")
        try:
            requests.delete(url, headers=headers, timeout=15)  # one-time: gone from the vault right after display
        except Exception:
            pass  # the secret still expires after 1 h; the row already records it as handed over
        kind = row.get("secretKind")
        html = render_template_string(REVEAL_PAGE, number=number, value=value, kind=kind,
                                      subject=row.get("subjectUserName"), display_name=reviewer_name)
        out = make_response(html)
        out.headers["Cache-Control"] = "no-store"
        out.headers["Pragma"] = "no-cache"
        return out

    return bp


ITSM_PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KnowledgeEngine v9 — Tickets ITSM</title>
<style>
  :root{
    --bg:#f7f4ef; --panel:#ffffff; --panel-2:#faf3ea; --line:#e8ddd0;
    --txt:#20211f; --muted:#7a7267; --accent:#e2703a; --accent-tint:#fbe9dc;
    --accent-blue:#5c85cf; --accent-blue-tint:#e9f0fb;
    --ok:#1f9d76; --ok-tint:#e3f5ee; --warn:#8a6a3f; --warn-tint:#fdf0c6;
    --danger:#b3372a; --danger-tint:#fbe4e0; --btn-bg:#14172a; --btn-text:#ffffff;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    background:var(--bg);color:var(--txt);line-height:1.55}
  header{padding:14px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;
    justify-content:space-between;gap:16px;background:var(--panel-2)}
  header .brand{display:flex;align-items:center;gap:10px}
  header .brand-dot{width:9px;height:9px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px var(--accent-tint)}
  header h1{margin:0;font-size:1.02rem;font-weight:650}
  header p{margin:2px 0 0;color:var(--muted);font-size:.78rem}
  header nav a{color:var(--txt);text-decoration:none;font-size:.85rem;margin-left:14px}
  header nav a.on{font-weight:650;border-bottom:2px solid var(--accent)}
  main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
  .tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
  .tabs a{padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:var(--panel);
    color:var(--txt);text-decoration:none;font-size:.85rem}
  .tabs a.on{background:var(--btn-bg);color:var(--btn-text);border-color:var(--btn-bg)}
  .tabs .n{opacity:.7;margin-left:4px}
  table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
  th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);font-size:.86rem;vertical-align:top}
  th{background:var(--panel-2);font-weight:600;color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.03em}
  tr:last-child td{border-bottom:0}
  td a{color:var(--txt);font-weight:600}
  .badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.75rem;font-weight:600;white-space:nowrap}
  .b-pending_review{background:var(--accent-blue-tint);color:var(--accent-blue)}
  .b-needs_human{background:var(--warn-tint);color:var(--warn)}
  .b-refused{background:var(--danger-tint);color:var(--danger)}
  .b-validated{background:var(--ok-tint);color:var(--ok)}
  .b-rejected,.b-handled_manually{background:var(--line);color:var(--muted)}
  .x-success{background:var(--ok-tint);color:var(--ok)}
  .x-partial,.x-dry_run,.x-running{background:var(--warn-tint);color:var(--warn)}
  .x-blocked,.x-error{background:var(--danger-tint);color:var(--danger)}
  .chip{display:inline-block;margin:2px 4px 2px 0;padding:2px 8px;border-radius:6px;font-size:.75rem;
    background:var(--danger-tint);color:var(--danger)}
  .chip.soft{background:var(--warn-tint);color:var(--warn)}
  .muted{color:var(--muted)}
  .grid{display:grid;grid-template-columns:1.2fr 1fr;gap:16px}
  @media (max-width:820px){.grid{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
  .card h2{margin:0 0 10px;font-size:.95rem}
  .kv{display:grid;grid-template-columns:150px 1fr;gap:4px 10px;font-size:.85rem}
  .kv div:nth-child(odd){color:var(--muted)}
  pre.desc{white-space:pre-wrap;font-family:inherit;font-size:.86rem;background:var(--panel-2);
    padding:10px;border-radius:8px;margin:8px 0 0;overflow-wrap:anywhere}
  .msg{padding:10px 12px;border-radius:8px;background:var(--accent-blue-tint);margin-bottom:14px;font-size:.86rem}
  .err{background:var(--danger-tint);color:var(--danger)}
  form .row{margin:10px 0}
  select,textarea{width:100%;font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;background:#fff}
  textarea{min-height:70px}
  .btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
  button{font:inherit;font-weight:650;border:0;border-radius:8px;padding:9px 14px;cursor:pointer}
  .btn-ok{background:var(--ok);color:#fff}
  .btn-no{background:var(--danger);color:#fff}
  .btn-neutral{background:var(--btn-bg);color:var(--btn-text)}
  .back{display:inline-block;margin-bottom:12px;color:var(--muted);text-decoration:none;font-size:.85rem}
  .rationale{border-left:3px solid var(--accent);padding:6px 10px;background:var(--accent-tint);border-radius:0 8px 8px 0;font-size:.88rem}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="brand-dot"></span>
    <div><h1>KnowledgeEngine v9 — Tickets ITSM</h1>
    <p>Propositions d'actions sur les tickets ServiceNow — rien n'est exécuté sans validation d'un agent.</p></div>
  </div>
  <nav><a href="/diag">Assistant</a><a class="on" href="/itsm">Tickets ITSM</a>{% if labels_nav %}<a href="/labels">Labellisation</a><a href="/dictionary">Dictionnaire</a>{% endif %}
    <span class="muted" style="margin-left:14px;font-size:.8rem">{{ display_name }}</span></nav>
</header>
<main>
{% if view == "queue" %}
  {% if error %}<div class="msg err">{{ error }}</div>{% endif %}
  <div class="tabs">
    {% for key, label in queues %}
      <a href="/itsm?q={{ key }}" class="{{ 'on' if key == active else '' }}">{{ label }}<span class="n">{{ counts.get(key, 0) }}</span></a>
    {% endfor %}
  </div>
  <table>
    <tr><th>Ticket</th><th>Demande</th><th>Appelant / bénéficiaire</th><th>Action proposée</th><th>Statut</th></tr>
    {% for r in rows %}
    <tr>
      <td><a href="/itsm/t/{{ r.RowKey }}">{{ r.RowKey }}</a></td>
      <td>{{ r.shortDescription }}
        {% for c in split(r.guardReasons) %}<br><span class="chip {{ '' if r.proposalStatus == 'refused' else 'soft' }}">{{ c | reason_label }}</span>{% endfor %}</td>
      <td>{{ r.subjectUserName }}{% if r.openedByUserName and r.openedByUserName != r.subjectUserName %}<br><span class="muted">demandé par {{ r.openedByUserName }}</span>{% endif %}</td>
      <td>{{ action_labels.get(r.proposalAction, r.proposalAction or '—') }}</td>
      <td>{% if r.executionStatus %}<span class="badge x-{{ r.executionStatus }}">{{ exec_labels.get(r.executionStatus, r.executionStatus) }}</span>
          {% elif r.reviewStatus %}<span class="badge b-{{ r.reviewStatus }}">{{ review_labels.get(r.reviewStatus, r.reviewStatus) }}</span>
          {% elif r.proposalStatus %}<span class="badge b-{{ r.proposalStatus }}">{{ status_labels.get(r.proposalStatus, r.proposalStatus) }}</span>
          {% else %}<span class="muted">en analyse</span>{% endif %}</td>
    </tr>
    {% else %}
    <tr><td colspan="5" class="muted">Aucun ticket dans cette file.</td></tr>
    {% endfor %}
  </table>
{% else %}
  <a class="back" href="/itsm?q={{ 'reviewed' if row.reviewStatus else (row.proposalStatus or 'new') }}">← Retour à la file</a>
  {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
  <div class="grid">
    <div class="card">
      <h2>{{ row.RowKey }} — {{ row.shortDescription }}</h2>
      <div class="kv">
        <div>Type</div><div>{{ 'Incident' if row.ticketType == 'incident' else 'Demande (RITM)' }}</div>
        <div>Demandeur</div><div>{{ row.openedByUserName }} <span class="muted">{{ row.openedByEmail }}</span></div>
        <div>{{ 'Appelant (ServiceNow)' if row.ticketType == 'incident' else 'Bénéficiaire (ServiceNow)' }}</div><div>{{ row.subjectUserName }} <span class="muted">{{ row.subjectEmail }}</span></div>
        <div>Manager (Entra)</div><div>{{ row.managerEmail or '—' }}</div>
        <div>Groupes (Entra)</div><div>{{ row.subjectGroups or '—' }}</div>
        <div>Rôles d'admin</div><div>{% if row.subjectPrivilegedRoles %}<span class="chip">{{ row.subjectPrivilegedRoles }}</span>{% else %}aucun{% endif %}</div>
      </div>
      <pre class="desc">{{ row.description }}</pre>
      {% if sn_instance and row.snSysId %}<p class="muted" style="font-size:.8rem">ServiceNow : {{ sn_instance }} — {{ row.RowKey }}</p>{% endif %}
    </div>
    <div class="card">
      <h2>Proposition</h2>
      <div class="kv">
        <div>Action</div><div><strong>{{ action_labels.get(row.proposalAction, row.proposalAction or '—') }}</strong></div>
        <div>Statut</div><div>{% if row.executionStatus %}<span class="badge x-{{ row.executionStatus }}">{{ exec_labels.get(row.executionStatus, row.executionStatus) }}</span>{% elif row.reviewStatus %}<span class="badge b-{{ row.reviewStatus }}">{{ review_labels.get(row.reviewStatus, row.reviewStatus) }}</span>{% elif row.proposalStatus %}<span class="badge b-{{ row.proposalStatus }}">{{ status_labels.get(row.proposalStatus, row.proposalStatus) }}</span>{% else %}<span class="muted">en analyse</span>{% endif %}</div>
        <div>Confiance</div><div>{{ row.proposalConfidence or '—' }}</div>
        {% if p.target_name %}<div>Compte visé</div><div>{{ p.target_name }}{% if p.target_is_requester == false %} <span class="chip">≠ demandeur</span>{% endif %}</div>{% endif %}
        {% if p.group_name %}<div>Groupe</div><div>{{ p.group_name }}</div>{% endif %}
        {% if p.license_sku_hint %}<div>Licence</div><div>{{ p.license_sku_hint }}</div>{% endif %}
      </div>
      {% if row.proposalRationale %}<p class="rationale">{{ row.proposalRationale }}</p>{% endif %}
      {% for c in split(row.guardReasons) %}<span class="chip {{ '' if row.proposalStatus == 'refused' else 'soft' }}">{{ c | reason_label }}</span>{% endfor %}

      {% if row.reviewStatus %}
        <div class="msg" style="margin-top:14px">
          <span class="badge b-{{ row.reviewStatus }}">{{ 'Validé' if row.reviewStatus == 'validated' and row.executionStatus else review_labels.get(row.reviewStatus, row.reviewStatus) }}</span>
          par {{ row.reviewedByName }} — {{ row.reviewedAtUtc[:16].replace('T', ' ') }} UTC
          {% if row.reviewComment %}<br><span class="muted">{{ row.reviewComment }}</span>{% endif %}
          {% if approved %}<br><span class="muted">Paramètres approuvés : {{ action_labels.get(approved.action, approved.action) }}{% if approved.group_name %} → {{ approved.group_name }}{% endif %}{% if approved.offboarding_steps %} → {% for st in approved.offboarding_steps %}{{ step_labels.get(st, st) }}{{ ', ' if not loop.last else '' }}{% endfor %}{% endif %}{% if approved.license_sku_hint %} → {{ approved.license_sku_hint }}{% endif %}</span>{% endif %}
        </div>
        {% if row.executionStatus %}
        <div class="msg" style="margin-top:10px">
          <span class="badge x-{{ row.executionStatus }}">{{ exec_labels.get(row.executionStatus, row.executionStatus) }}</span>
          {% if row.executedAtUtc %}<span class="muted"> — {{ row.executedAtUtc[:16].replace('T', ' ') }} UTC</span>{% endif %}
          {% for st in exec_log %}<br>{{ '✓' if st.ok else '✗' }} {{ st.step }} <span class="muted">{{ st.detail }}</span>{% endfor %}
          {% if row.executionStatus == 'success' and row.ticketType == 'ritm' %}<br><span class="muted">Demande clôturée dans ServiceNow.</span>{% endif %}
          {% if row.executionStatus == 'success' and row.ticketType == 'incident' %}<br><span class="muted">Incident résolu dans ServiceNow.</span>{% endif %}
          {% if row.secretRef %}
            {% if row.secretRevealedAtUtc %}
              <br><span class="muted">{{ 'Code temporaire (TAP)' if row.secretKind == 'tap' else 'Mot de passe temporaire' }} remis par {{ row.secretRevealedByName }} — {{ row.secretRevealedAtUtc[:16].replace('T', ' ') }} UTC. Supprimé du coffre.</span>
            {% elif me == row.reviewedById %}
              <form method="post" action="/itsm/t/{{ row.RowKey }}/reveal" style="margin-top:10px">
                <button class="btn-neutral">Afficher le {{ 'code temporaire (TAP)' if row.secretKind == 'tap' else 'mot de passe temporaire' }} — une seule fois</button>
              </form>
            {% else %}
              <br><span class="muted">Code à remettre par l'agent validateur ({{ row.reviewedByName }}).</span>
            {% endif %}
          {% endif %}
        </div>
        {% endif %}
      {% elif row.proposalStatus == 'pending_review' %}
        <form method="post" action="/itsm/t/{{ row.RowKey }}/decision">
          <input type="hidden" name="proposed_at" value="{{ row.proposedAtUtc }}">
          {% if row.proposalAction == 'group_add' %}
            <div class="row"><label>Groupe<select name="group_name">
              {% for g in allowed_groups %}<option value="{{ g }}" {{ 'selected' if g == p.group_name else '' }}>{{ g }}</option>{% endfor %}
            </select></label></div>
          {% elif row.proposalAction == 'offboarding' %}
            <div class="row">Étapes (décochez celles à ne pas exécuter) :
              {% for s in p.offboarding_steps or [] %}<br><label><input type="checkbox" name="steps" value="{{ s }}" checked> {{ step_labels.get(s, s) }}</label>{% endfor %}
            </div>
          {% endif %}
          <div class="row"><label>Commentaire (obligatoire pour rejeter)<textarea name="comment"></textarea></label></div>
          <div class="btns">
            <button class="btn-ok" name="decision" value="validate">Valider</button>
            <button class="btn-no" name="decision" value="reject">Rejeter</button>
          </div>
        </form>
      {% elif row.proposalStatus in ('needs_human', 'refused') %}
        <form method="post" action="/itsm/t/{{ row.RowKey }}/decision">
          <input type="hidden" name="proposed_at" value="{{ row.proposedAtUtc }}">
          <p class="muted" style="font-size:.85rem">{% if row.proposalStatus == 'refused' %}Bloqué par un garde-fou de sécurité : aucune exécution automatique possible.{% else %}Nécessite un traitement humain.{% endif %}</p>
          <div class="row"><label>Commentaire<textarea name="comment"></textarea></label></div>
          <div class="btns"><button class="btn-neutral" name="decision" value="manual">Marquer comme traité manuellement</button></div>
        </form>
      {% endif %}
    </div>
  </div>
{% endif %}
</main>
</body>
</html>
"""


REVEAL_PAGE = """
<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Code temporaire — {{ number }}</title>
<style>
  body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f7f4ef;color:#20211f}
  main{max-width:640px;margin:60px auto;padding:0 16px}
  .card{background:#fff;border:1px solid #e8ddd0;border-radius:12px;padding:24px}
  .secret{font-family:ui-monospace,Consolas,monospace;font-size:1.6rem;letter-spacing:.06em;background:#faf3ea;
    border:1px dashed #e2703a;border-radius:10px;padding:16px;text-align:center;margin:18px 0;user-select:all}
  .warn{background:#fbe4e0;color:#b3372a;border-radius:8px;padding:10px 12px;font-size:.88rem}
  a{color:#20211f}
</style></head><body><main><div class="card">
  <h2 style="margin-top:0">{{ number }} — {{ 'Temporary Access Pass' if kind == 'tap' else 'Mot de passe temporaire' }} pour {{ subject }}</h2>
  <div class="secret">{{ value }}</div>
  <p style="font-size:.9rem">{% if kind == 'tap' %}Code à usage unique, valable 60 minutes : l'utilisateur s'en sert pour se connecter et réenregistrer Microsoft Authenticator.{% else %}L'utilisateur devra changer ce mot de passe à sa première connexion.{% endif %}</p>
  <div class="warn">Ce code ne sera plus jamais affiché : il vient d'être supprimé du coffre. Communiquez-le à l'utilisateur par un canal vérifié (rappel sur son numéro connu, en personne) — jamais dans le ticket ni par e-mail.</div>
  <p><a href="/itsm/t/{{ number }}">← Retour au ticket</a></p>
</div></main></body></html>
"""
