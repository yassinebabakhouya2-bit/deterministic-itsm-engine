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
#   - Search Index Data Reader        on the Search service
#   - Cognitive Services OpenAI User  on the Foundry account
#   - Storage Table Data Contributor  on the storage account (conversations)
#
# Client resolution (Jalon 5 — replaces the Jalon 4 ALLOWED_CLIENTS
# stopgap): the client(s) a given request may query are resolved from the
# X-MS-CLIENT-PRINCIPAL claims Easy Auth itself attaches to every
# authenticated request (tenant, then Entra group) — never from a value
# the browser posted. See app/auth.py and project memory
# jalon5-auth-isolation.md for the full design. Deny-by-default: an
# unrecognized tenant, or a tenant with no matching group, sees no client
# at all, not a fallback.
#
# Design note (2026-09-18 -- richer UI, per Yassine's "ça ne reflète pas
# le projet" / "c'est encombré" feedback):
#   - The model's answer is rendered as Markdown -> sanitized HTML instead
#     of one flat paragraph (see _render_answer_html). SECURITY: the
#     answer text is LLM output grounded in retrieved KB/call content,
#     which is untrusted (a malicious KB doc or transcript could in
#     principle smuggle HTML through a prompt-injection-echoed string) --
#     markdown's raw-HTML passthrough alone is not safe to mark `| safe`
#     for Jinja, so the rendered HTML is run through nh3 (allowlist, ZERO
#     attributes permitted -- no href/src/on*) before being trusted.
#   - A confidence badge (_confidence) buckets the reranker score.
#     DISPLAY-ONLY: retrieve_hierarchy() in orchestration/answer.py still
#     decides primary/annex (since 2026-09-21, by two modality-filtered
#     searches), never by this bucket -- Microsoft's
#     guidance against fine-grained score thresholds applies to that
#     decision, not to a purely descriptive badge.
#   - A modality summary line (_modality_summary) states in one sentence
#     what corroborated the answer.
#
# Design note (2026-09-18, later -- saved conversations + sidebar):
# Yassine's feedback on the first history pass: a vertical, ever-collapsing
# list where an opened item only shows a truncated preview doesn't let you
# actually revisit a past exchange -- he wants a real sidebar, each
# conversation reopenable in full. A per-session cookie can't hold that (a
# single source excerpt alone can run ~2000 chars -- see
# orchestration/answer.py's SplitSkill maximumPageLength -- multiplied by
# several turns and several conversations, it blows well past what a
# browser reliably keeps in a ~4KB cookie). So conversations now live
# server-side in two Azure Tables on the SAME storage account the KB
# containers already use, read/written with the Web App's managed
# identity (axiom A5, zero keys) -- consistent with how Search's own
# identity already reads Blob:
#   - convindex: PartitionKey = "<clientId>:<userId>", RowKey = a
#     conversation id encoded so ascending RowKey order = newest first
#     (see _new_conversation_id) -- lists a user's conversations for the
#     currently selected client without a server-side sort.
#   - convturns: PartitionKey = conversation id, RowKey = zero-padded turn
#     index -- every turn of one conversation, in order, full detail
#     (including source excerpts -- no cookie-size constraint here, a
#     String property caps at 64KB, comfortably more than a few chunks).
# Ownership check is structural, not a permission flag: a conversation is
# looked up with get_entity(partition_key=<OUR computed clientId:userId>,
# row_key=<id from the URL>) -- an id belonging to another user simply
# isn't in that partition, so a guessed id 404s by construction, the same
# "never trust an id from the URL alone" posture as the rest of this app.
# userId comes from the `oid` claim Easy Auth attaches (see
# app/auth.py:resolve_user_id) -- never something the browser posted.
#
# Resilience: if the Storage Table role hasn't propagated yet (or Table
# access fails for any reason), answering still works -- persistence is
# attempted in a try/except and silently degrades to a one-off, unsaved
# answer (see _handle) rather than a 500. The sidebar just stays empty
# until the role is actually in effect.
#
# Design note (2026-09-18, later still -- audio excerpts are NOT safe to
# show verbatim): the "extrait pertinent repliable" pivot (see
# orchestration/answer.py's 2026-09-18 excerpt design note) assumed the
# retrieved chunk was safe because it's exactly what grounded the answer,
# nothing from the rest of the call. Live testing proved that assumption
# wrong: a chunk can itself capture a moment where an agent has the
# customer spell out a new password letter by letter -- speech-to-text
# and RAG chunking have no notion of "this segment is a credential,
# redact it", and no regex-based scrubber is reliable enough on free-form
# dictated French to trust here. So the excerpt disclosure is now
# INVERTED: shown (verbatim) only for sourceType NOT in (audio, video) --
# authored KB documentation, which is not spoken customer data and is
# also what Yassine specifically asked to be able to verify faithfully.
# Audio/video sources still show their badge, title and "utilisée" flag
# (so multimodality stays visible) but never their content -- a short
# ".excerpt-hidden" note explains why instead of just silently omitting
# it. This is strictly more conservative than the original excerpt
# design, never less.
#
# Design note (2026-09-19 -- audio annexes were becoming useless): hiding
# audio/video content outright (above) closed the leak, but Yassine
# pointed out it also defeated the point of transcribing and indexing
# calls at all -- a badge and a title with nothing behind it. The fix is
# NOT to show the raw excerpt more carefully; it's to never show raw call
# text at all and instead show `safe_summary` (orchestration/answer.py,
# same date): a short paraphrase the model writes in its own words when
# it actually used that call, explicitly instructed to describe what was
# useful without ever quoting a dictated credential -- backstopped by
# answer.py's _looks_sensitive() heuristic, which suppresses the summary
# entirely (falls back to the same ".excerpt-hidden" note) if it still
# looks like character-by-character dictation slipped through. This is
# what actually makes the audio pipeline worth having: real information
# from the call, never the call's own words.
# =====================================================================
import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import markdown as _markdown_lib
import nh3
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.data.tables import TableServiceClient
from azure.identity import DefaultAzureCredential
from flask import Flask, redirect, render_template_string, request, url_for

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestration"))
from answer import (  # noqa: E402 -- reuse orchestration logic (axiom A4), not a duplicate
    analyze_screenshot_query_core_keyless,
    answer_query_core_keyless,
    build_aoai_client_keyless,
    load_engine_config,
)

from auth import (  # noqa: E402
    build_access_maps,
    parse_client_principal,
    resolve_allowed_clients,
    resolve_user_id,
)

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

# Storage account holding every client's kb-<client> container AND (Jalon 7+)
# the conversation tables below -- one shared account, per-purpose isolation
# is the container/table, exactly like knowledge.index is the isolation
# boundary on the Search side.
_STORAGE_ACCOUNT = "stknowledgeengine2v9"
_TABLE_ENDPOINT = f"https://{_STORAGE_ACCOUNT}.table.core.windows.net"
_CONV_INDEX_TABLE = "convindex"
_CONV_TURNS_TABLE = "convturns"

_table_service = TableServiceClient(endpoint=_TABLE_ENDPOINT, credential=_credential)


def _ensure_tables():
    for name in (_CONV_INDEX_TABLE, _CONV_TURNS_TABLE):
        try:
            _table_service.create_table(name)
        except ResourceExistsError:
            pass
        except Exception:
            # Role not propagated yet, network hiccup at startup, etc. --
            # never crash app startup over this; _list_conversations /
            # _create_conversation below fail soft on their own.
            pass


_ensure_tables()
_conv_index_client = _table_service.get_table_client(_CONV_INDEX_TABLE)
_conv_turns_client = _table_service.get_table_client(_CONV_TURNS_TABLE)

app = Flask(__name__)

HISTORY_MAX = 30  # conversations listed in the sidebar

EXAMPLE_QUESTIONS = [
    "Comment réinitialiser mon mot de passe ?",
    "Comment ajouter une imprimante avec Printer Logic ?",
    "Comment débloquer une URL sur Palo Alto ?",
    "Comment configurer Outlook sur iPhone ?",
]

# Modality badge (Jalon 7+) -- sourceType comes straight from the index
# (search/skillset.template.json projects it from blob metadata
# x-ms-meta-sourcetype, set by the producing Logic App; absent/None for
# ordinary KB documents, which is why the default below is the plain
# document icon rather than an explicit "document" tag).
_SOURCE_BADGES = {"audio": "🎧", "video": "🎬"}


def _source_badge(source_type):
    return _SOURCE_BADGES.get(source_type, "📄")


app.jinja_env.filters["badge"] = _source_badge

# Allowlist for the sanitized answer HTML -- deliberately no attributes at
# all (no href/src/on*), see module docstring.
_MD_ALLOWED_TAGS = {"p", "strong", "em", "ul", "ol", "li", "br", "code", "pre", "blockquote", "h3", "h4"}


def _render_answer_html(text):
    html = _markdown_lib.markdown(text or "", extensions=["nl2br", "sane_lists"])
    return nh3.clean(html, tags=_MD_ALLOWED_TAGS, attributes={})


def _confidence(score):
    """Coarse, DISPLAY-ONLY confidence bucket -- see module docstring."""
    if score is None:
        return "inconnue", "unknown"
    if score >= 2.5:
        return "Élevée", "high"
    if score >= 1.5:
        return "Moyenne", "medium"
    return "Faible", "low"


def _modality_summary(primary, related):
    if not primary:
        return ""
    media = [s for s in related if s.get("sourceType") in ("audio", "video")]
    kb_annex = [s for s in related if s.get("sourceType") not in ("audio", "video")]
    kind = "un appel support" if primary.get("sourceType") in ("audio", "video") else "un document KB"
    parts = [f"Basé sur {kind}"]
    if media:
        parts.append(f"corroboré par {len(media)} appel{'s' if len(media) > 1 else ''}")
    if kb_annex:
        parts.append(f"{len(kb_annex)} document{'s' if len(kb_annex) > 1 else ''} KB en complément")
    return " · ".join(parts)


def _truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------
# Conversation persistence (Azure Table Storage) -- see module docstring.
# ---------------------------------------------------------------------


def _new_conversation_id() -> str:
    # 10-digit reverse-timestamp prefix -> ascending RowKey order (Table
    # Storage's only free ordering within a partition) reads newest-first.
    # The uuid suffix avoids a collision when two conversations start in
    # the same second.
    return f"{9999999999 - int(time.time()):010d}-{uuid.uuid4().hex[:8]}"


def _user_partition(client_id: str, user_id: str) -> str:
    return f"{client_id}:{user_id}"


def _list_conversations(client_id: str, user_id: str):
    if not client_id or not user_id:
        return []
    try:
        pk = _user_partition(client_id, user_id)
        entities = _conv_index_client.query_entities(
            query_filter="PartitionKey eq @pk", parameters={"pk": pk}
        )
        return [dict(e) for e in entities][:HISTORY_MAX]
    except Exception:
        return []  # Table not reachable/authorized yet -- sidebar just stays empty


def _create_conversation(client_id: str, user_id: str, title: str) -> str:
    conv_id = _new_conversation_id()
    now = datetime.now(timezone.utc).isoformat()
    _conv_index_client.create_entity(
        {
            "PartitionKey": _user_partition(client_id, user_id),
            "RowKey": conv_id,
            "clientId": client_id,
            "title": _truncate(title, 80),
            "createdAt": now,
            "updatedAt": now,
            "turnCount": 0,
        }
    )
    return conv_id


def _append_turn(conv_id: str, entry: dict) -> int:
    existing = list(
        _conv_turns_client.query_entities(
            query_filter="PartitionKey eq @pk", parameters={"pk": conv_id}, select=["RowKey"]
        )
    )
    idx = len(existing)
    _conv_turns_client.create_entity(
        {
            "PartitionKey": conv_id,
            "RowKey": f"{idx:05d}",
            "query": entry["query"],
            "answer_html": entry["answer_html"],
            "ambiguous": bool(entry.get("ambiguous")),
            "unanswerable_reason": entry.get("unanswerable_reason") or "",
            "confidence_label": entry["confidence_label"],
            "confidence_level": entry["confidence_level"],
            "modality_summary": entry.get("modality_summary") or "",
            "primary_source_json": json.dumps(entry.get("primary_source")),
            "related_sources_json": json.dumps(entry.get("related_sources") or []),
            "screen_reading": entry.get("screen_reading") or "",
            "detected_error_codes_json": json.dumps(entry.get("detected_error_codes") or []),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
    )
    return idx


def _touch_conversation(client_id: str, user_id: str, conv_id: str, turn_count: int):
    _conv_index_client.update_entity(
        {
            "PartitionKey": _user_partition(client_id, user_id),
            "RowKey": conv_id,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
            "turnCount": turn_count,
        },
        mode="merge",
    )


def _get_conversation_meta(client_id: str, user_id: str, conv_id: str):
    """Structural ownership check -- see module docstring. Returns None
    both when the conversation doesn't exist AND when it belongs to
    someone else, indistinguishably (never leaks which)."""
    try:
        return _conv_index_client.get_entity(
            partition_key=_user_partition(client_id, user_id), row_key=conv_id
        )
    except ResourceNotFoundError:
        return None
    except Exception:
        return None


def _load_turns(conv_id: str):
    try:
        entities = _conv_turns_client.query_entities(
            query_filter="PartitionKey eq @pk", parameters={"pk": conv_id}
        )
        rows = sorted(entities, key=lambda e: e["RowKey"])
    except Exception:
        return []
    turns = []
    for e in rows:
        turns.append(
            {
                "query": e.get("query"),
                "answer_html": e.get("answer_html"),
                "ambiguous": e.get("ambiguous"),
                "unanswerable_reason": e.get("unanswerable_reason") or None,
                "confidence_label": e.get("confidence_label"),
                "confidence_level": e.get("confidence_level"),
                "modality_summary": e.get("modality_summary"),
                "primary_source": json.loads(e.get("primary_source_json") or "null"),
                "related_sources": json.loads(e.get("related_sources_json") or "[]"),
                "screen_reading": e.get("screen_reading") or None,
                "detected_error_codes": json.loads(e.get("detected_error_codes_json") or "[]"),
            }
        )
    return turns


PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KnowledgeEngine v9 — Démo</title>
<style>
  :root{
    --bg:#0b1120; --panel:#161f33; --panel-2:#101827; --line:#26314a;
    --txt:#e7edf7; --muted:#8b98b3; --accent:#38bdf8; --accent-dim:#0c4a6e;
    --ok:#34d399; --warn:#fbbf24; --danger:#f87171;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    background:var(--bg);color:var(--txt);line-height:1.55;
    display:flex;flex-direction:column;overflow:hidden}
  header{flex-shrink:0;padding:14px 24px;border-bottom:1px solid var(--line);display:flex;
    align-items:center;justify-content:space-between;gap:16px;background:var(--panel-2)}
  header .brand{display:flex;align-items:center;gap:10px}
  header .brand-dot{width:9px;height:9px;border-radius:50%;background:var(--accent);
    box-shadow:0 0 10px var(--accent)}
  header h1{margin:0;font-size:1.02rem;font-weight:650;letter-spacing:.01em}
  header p{margin:2px 0 0;color:var(--muted);font-size:.78rem}
  .logout{flex-shrink:0;color:var(--muted);text-decoration:none;font-size:.8rem;
    border:1px solid var(--line);border-radius:7px;padding:6px 12px;white-space:nowrap;
    transition:color .15s,border-color .15s}
  .logout:hover{color:var(--txt);border-color:var(--accent)}
  .layout{display:flex;flex:1;min-height:0;overflow:hidden}
  .sidebar{width:250px;flex-shrink:0;padding:16px 10px;border-right:1px solid var(--line);
    background:var(--panel-2);overflow-y:auto;display:flex;flex-direction:column}
  .sidebar-new{display:flex;align-items:center;justify-content:center;gap:6px;
    background:var(--accent);color:#04121f;font-weight:650;text-decoration:none;
    border-radius:8px;padding:10px 12px;font-size:.85rem;margin-bottom:12px;
    transition:opacity .15s;flex-shrink:0}
  .sidebar-new:hover{opacity:.88}
  .sidebar-section-label{font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;
    color:var(--muted);padding:4px 10px 6px}
  .conv-list{display:flex;flex-direction:column;gap:2px}
  .conv-link{display:block;padding:9px 10px;border-radius:8px;color:var(--muted);
    text-decoration:none;border-left:2px solid transparent;transition:background .15s,color .15s}
  .conv-link-title{font-size:.82rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    color:inherit;font-weight:500}
  .conv-link-meta{font-size:.7rem;color:var(--muted);margin-top:1px;opacity:.8}
  .conv-link:hover{background:var(--panel);color:var(--txt)}
  .conv-link.active{background:var(--panel);color:var(--txt);border-left-color:var(--accent)}
  .conv-empty{color:var(--muted);font-size:.78rem;padding:8px 10px}
  .chat{flex:1;min-width:0;display:flex;flex-direction:column;min-height:0}
  .messages{flex:1;overflow-y:auto;padding:22px 24px 12px}
  .messages-inner{max-width:760px;margin:0 auto}
  @media (max-width:800px){
    body{height:auto;overflow:visible}
    .layout{flex-direction:column;overflow:visible}
    .sidebar{width:100%;border-right:0;border-bottom:1px solid var(--line);max-height:180px}
    .chat{overflow:visible}
    .messages{overflow:visible}
    .composer{position:sticky;bottom:0}
  }
  .denied{padding:16px;border:1px solid #7f1d1d;background:#2a1010;
    border-radius:10px;color:#fca5a5;font-size:.9rem;max-width:760px;margin:0 auto}
  .error{padding:12px 14px;border:1px solid #7f1d1d;background:#2a1010;
    border-radius:8px;color:#fca5a5;font-size:.88rem;margin-bottom:16px}
  .welcome{padding:40px 8px 20px;text-align:center}
  .welcome-title{font-size:1.3rem;font-weight:650;margin-bottom:8px}
  .welcome-sub{color:var(--muted);font-size:.9rem;max-width:480px;margin:0 auto;line-height:1.6}
  .privacy-pledge{display:inline-flex;align-items:center;gap:6px;margin-top:16px;
    padding:7px 14px;border-radius:999px;background:var(--panel);border:1px solid var(--line);
    color:var(--muted);font-size:.76rem}
  .examples{display:flex;flex-wrap:wrap;gap:8px;margin-top:22px;justify-content:center}
  .examples form{background:none;border:0;padding:0;display:inline;margin:0}
  .chip{margin:0;background:var(--panel);color:var(--muted);border:1px solid var(--line);
    border-radius:999px;padding:8px 15px;font-size:.82rem;font-weight:400;cursor:pointer;
    transition:color .15s,border-color .15s}
  .chip:hover{color:var(--txt);border-color:var(--accent)}
  .turn{margin-bottom:26px}
  .turn-q{display:flex;align-items:baseline;gap:8px;margin-bottom:10px}
  .turn-q-label{font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;
    color:var(--muted);flex-shrink:0}
  .turn-q p{margin:0;font-weight:600;font-size:.98rem;color:var(--txt)}
  .turn-a{padding:16px 18px;border:1px solid var(--line);background:var(--panel);
    border-radius:12px;border-left:3px solid var(--accent-dim)}
  .turn-a.ambiguous{border-left-color:var(--warn)}
  .turn-a-label{font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;
    color:var(--accent);margin-bottom:8px;font-weight:650}
  .answer-text p{margin:0 0 10px}
  .answer-text p:last-child{margin-bottom:0}
  .answer-text ul,.answer-text ol{margin:6px 0 10px;padding-left:22px}
  .answer-text li{margin:3px 0}
  .answer-text code{background:var(--panel-2);padding:1px 5px;border-radius:4px;font-size:.85em}
  .ambiguous-note{margin-top:10px;padding:8px 10px;border-radius:7px;background:#3a2e05;
    color:var(--warn);font-size:.83rem}
  .meta-row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:14px;font-size:.8rem}
  .confidence{display:inline-flex;align-items:center;gap:5px;padding:3px 11px;border-radius:999px;
    font-weight:600;font-size:.74rem;white-space:nowrap}
  .confidence .dot{width:6px;height:6px;border-radius:50%;display:inline-block}
  .confidence-high{background:#052e1e;color:#34d399}
  .confidence-high .dot{background:#34d399}
  .confidence-medium{background:#3a2e05;color:#fbbf24}
  .confidence-medium .dot{background:#fbbf24}
  .confidence-low{background:#3a0d0d;color:#fca5a5}
  .confidence-low .dot{background:#fca5a5}
  .confidence-unknown{background:var(--panel-2);color:var(--muted)}
  .confidence-unknown .dot{background:var(--muted)}
  .modality-summary{color:var(--muted)}
  .sources{margin-top:16px;display:flex;flex-direction:column;gap:12px}
  .source-group-label{display:flex;align-items:center;gap:6px;font-size:.72rem;
    text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin-bottom:6px}
  .glabel-icon{font-size:.9rem}
  .group-primary .source-group-label{color:#7dd3fc}
  .group-media-annex .source-group-label{color:var(--muted)}
  .src-card{padding:9px 11px;border:1px solid var(--line);border-radius:9px;margin-top:6px;
    font-size:.85rem;background:var(--panel-2)}
  .src-card:first-child{margin-top:0}
  .src-card.primary{border-color:var(--accent-dim);background:#0c1c2e}
  .src-card.secondary{opacity:.85}
  .src-card-title{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
  .src-title{flex:1;min-width:0}
  .src-card.used .src-title{color:var(--ok)}
  .tag.used-tag{display:inline-block;font-size:.68rem;padding:1px 7px;border-radius:999px;
    background:#052e1e;color:var(--ok)}
  .excerpt{margin-top:7px}
  .excerpt summary{cursor:pointer;font-size:.8rem;color:var(--accent);list-style:none}
  .excerpt summary::-webkit-details-marker{display:none}
  .excerpt summary:hover{opacity:.85}
  .excerpt-hidden{margin-top:7px;font-size:.78rem;color:var(--muted);font-style:italic;
    display:flex;align-items:center;gap:5px}
  .safe-summary{margin-top:7px;padding:9px 11px;border-radius:8px;background:#0c1c2e;
    border:1px solid var(--accent-dim);color:var(--txt);font-size:.83rem;line-height:1.5}
  .excerpt pre{white-space:pre-wrap;word-break:break-word;margin:7px 0 0;padding:10px 12px;
    background:var(--bg);border:1px solid var(--line);border-radius:7px;font-size:.8rem;
    color:var(--txt);max-height:240px;overflow:auto}
  .no-sources{margin-top:14px;color:var(--muted);font-size:.8rem;font-style:italic}
  .composer{flex-shrink:0;border-top:1px solid var(--line);background:var(--panel-2);
    padding:14px 24px 18px}
  .composer-inner{max-width:760px;margin:0 auto}
  .composer-row{display:flex;gap:10px;align-items:flex-end;background:var(--panel);
    border:1px solid var(--line);border-radius:14px;padding:8px 8px 8px 14px;
    transition:border-color .15s}
  .composer-row:focus-within{border-color:var(--accent)}
  .client-select{background:var(--panel-2);color:var(--muted);border:1px solid var(--line);
    border-radius:8px;padding:7px 9px;font-size:.78rem;font-family:inherit;flex-shrink:0;
    max-width:120px;align-self:center}
  .composer textarea{flex:1;min-width:0;background:transparent;color:var(--txt);border:0;
    resize:vertical;font-size:.94rem;font-family:inherit;padding:8px 0;max-height:220px;
    min-height:44px;overflow-y:auto}
  .composer textarea:focus{outline:none}
  .send-btn{flex-shrink:0;background:var(--accent);color:#04121f;border:none;border-radius:10px;
    padding:9px 18px;font-weight:650;cursor:pointer;font-size:.86rem;align-self:flex-end;
    transition:opacity .15s}
  .send-btn:hover{opacity:.88}
  .screenshot-btn{flex-shrink:0;align-self:flex-end;cursor:pointer;font-size:1.1rem;
    padding:9px 10px;border-radius:10px;border:1px solid var(--line);background:var(--panel-2);
    transition:opacity .15s;line-height:1}
  .screenshot-btn:hover{opacity:.85}
  .screenshot-input{position:absolute;width:1px;height:1px;overflow:hidden;opacity:0}
  .screen-reading-note{margin:0 0 8px;font-size:.8rem;color:var(--muted);font-style:italic;
    background:var(--panel-2);border-radius:8px;padding:7px 10px}
</style>
</head>
<body>
<header>
  <div class="brand">
    <span class="brand-dot"></span>
    <div>
      <h1>KnowledgeEngine v9 — Assistant support IT</h1>
      <p>Connecté via Entra ID — le client affiché ci-dessous est déterminé automatiquement par votre organisation.</p>
    </div>
  </div>
  <a class="logout" href="/.auth/logout?post_logout_redirect_uri=/">Se déconnecter</a>
</header>
<div class="layout">
  <aside class="sidebar">
    <a class="sidebar-new" href="/?client_id={{ client_id }}">＋ Nouvelle conversation</a>
    <div class="sidebar-section-label">Conversations</div>
    <div class="conv-list">
      {% for c in conversations %}
      <a class="conv-link {% if c.RowKey == active_conversation_id %}active{% endif %}"
         href="/c/{{ c.RowKey }}?client_id={{ client_id }}">
        <div class="conv-link-title">{{ c.title }}</div>
        <div class="conv-link-meta">{{ c.turnCount }} échange{{ "s" if c.turnCount and c.turnCount > 1 else "" }}</div>
      </a>
      {% else %}
      <div class="conv-empty">Aucune conversation enregistrée.</div>
      {% endfor %}
    </div>
  </aside>
  <div class="chat">
  {% if not allowed_clients %}
  <div class="messages"><div class="messages-inner">
    <div class="denied">
      Accès refusé : aucun client n'est associé à votre compte sur cette instance.
      Contactez votre administrateur si vous pensez que c'est une erreur.
    </div>
  </div></div>
  {% else %}
  <div class="messages"><div class="messages-inner">
    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}

    {% if not turns %}
    <div class="welcome">
      <div class="welcome-title">Comment puis-je vous aider ?</div>
      <div class="welcome-sub">Posez une question sur vos procédures IT — la réponse combine votre
        base de connaissances (source principale) et l'historique des appels support (en complément).</div>
      <div class="privacy-pledge">🔒 Le contenu brut des appels n'est jamais affiché, seulement le texte KB</div>
      {% if not active_conversation_id %}
      <div class="examples">
        {% for ex in example_questions %}
        <form method="post">
          <input type="hidden" name="client_id" value="{{ client_id }}">
          <button type="submit" name="query" value="{{ ex }}" class="chip">{{ ex }}</button>
        </form>
        {% endfor %}
      </div>
      {% endif %}
    </div>
    {% endif %}

    {% for t in turns %}
    <div class="turn">
      <div class="turn-q">
        <span class="turn-q-label">Vous</span>
        <p>{{ t.query }}</p>
      </div>
      <div class="turn-a {% if t.ambiguous %}ambiguous{% endif %}">
        <div class="turn-a-label">Assistant</div>
        {% if t.screen_reading %}
        <div class="screen-reading-note">📷 Capture lue : {{ t.screen_reading }}{% if t.detected_error_codes %} — codes detectes : {{ t.detected_error_codes | join(", ") }}{% endif %}</div>
        {% endif %}
        <div class="answer-text">{{ t.answer_html | safe }}</div>
        {% if t.ambiguous %}
        <div class="ambiguous-note">⚠ Réponse ambiguë{% if t.unanswerable_reason %} — {{ t.unanswerable_reason }}{% endif %}</div>
        {% endif %}
        <div class="meta-row">
          <span class="confidence confidence-{{ t.confidence_level }}"><span class="dot"></span>Confiance {{ t.confidence_label }}</span>
          {% if t.modality_summary %}<span class="modality-summary">{{ t.modality_summary }}</span>{% endif %}
        </div>

        {% if t.primary_source or t.related_sources %}
        <div class="sources">
          {% if t.primary_source %}
          <div class="source-group group-primary">
            <div class="source-group-label">
              {% if t.primary_source.sourceType not in ("audio", "video") %}
              <span class="glabel-icon">📄</span> Source principale — base de connaissances
              {% else %}
              <span class="glabel-icon">⚠️</span> Aucun document KB trouvé — réponse basée sur un appel
              {% endif %}
            </div>
            <div class="src-card primary {% if t.primary_source.used %}used{% endif %}">
              <div class="src-card-title">
                {{ t.primary_source.sourceType | badge }}
                <span class="src-title">{{ t.primary_source.title }}</span>
                {% if t.primary_source.used %}<span class="tag used-tag">utilisée</span>{% endif %}
              </div>
              {% if t.primary_source.sourceType not in ("audio", "video") and t.primary_source.excerpt %}
              <details class="excerpt">
                <summary>▸ voir l'extrait utilisé</summary>
                <pre>{{ t.primary_source.excerpt }}</pre>
              </details>
              {% elif t.primary_source.safe_summary %}
              <div class="safe-summary">🛡️ {{ t.primary_source.safe_summary }}</div>
              {% elif t.primary_source.sourceType in ("audio", "video") %}
              <div class="excerpt-hidden">🔒 contenu de l'appel non affiché (confidentialité)</div>
              {% endif %}
            </div>
          </div>
          {% endif %}

          {% set kb_annexes = t.related_sources | rejectattr("sourceType", "in", ["audio", "video"]) | list %}
          {% set media_annexes = t.related_sources | selectattr("sourceType", "in", ["audio", "video"]) | list %}

          {% if kb_annexes %}
          <div class="source-group group-kb-annex">
            <div class="source-group-label"><span class="glabel-icon">📄</span> Documents KB complémentaires</div>
            {% for s in kb_annexes %}
            <div class="src-card {% if s.used %}used{% endif %}">
              <div class="src-card-title">
                {{ s.sourceType | badge }}
                <span class="src-title">{{ s.title }}</span>
                {% if s.used %}<span class="tag used-tag">utilisée</span>{% endif %}
              </div>
              {% if s.excerpt %}
              <details class="excerpt">
                <summary>▸ voir l'extrait utilisé</summary>
                <pre>{{ s.excerpt }}</pre>
              </details>
              {% endif %}
            </div>
            {% endfor %}
          </div>
          {% endif %}

          {% if media_annexes %}
          <div class="source-group group-media-annex">
            <div class="source-group-label"><span class="glabel-icon">🔒</span> Sources secondaires — appels &amp; vidéos</div>
            {% for s in media_annexes %}
            <div class="src-card secondary {% if s.used %}used{% endif %}">
              <div class="src-card-title">
                {{ s.sourceType | badge }}
                <span class="src-title">{{ s.title }}</span>
                {% if s.used %}<span class="tag used-tag">utilisée</span>{% endif %}
              </div>
              {% if s.safe_summary %}
              <div class="safe-summary">🛡️ {{ s.safe_summary }}</div>
              {% else %}
              <div class="excerpt-hidden">🔒 contenu non affiché (confidentialité)</div>
              {% endif %}
            </div>
            {% endfor %}
          </div>
          {% endif %}
        </div>
        {% else %}
        <div class="no-sources">Aucune source retrouvée pour cette question.</div>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </div></div>

  <form method="post" class="composer" enctype="multipart/form-data">
    <input type="hidden" name="conversation_id" value="{{ active_conversation_id or '' }}">
    <div class="composer-inner">
      <div class="composer-row">
        <select name="client_id" class="client-select" title="Client">
          {% for c in allowed_clients %}
          <option value="{{ c }}" {% if c == client_id %}selected{% endif %}>{{ c }}</option>
          {% endfor %}
        </select>
        <textarea name="query" rows="2" placeholder="Pose ta question..." required>{{ query }}</textarea>
        <label class="screenshot-btn" title="Joindre une capture d'ecran">
          📷<input type="file" name="screenshot" accept="image/*" class="screenshot-input">
        </label>
        <button type="submit" class="send-btn">Envoyer</button>
      </div>
    </div>
  </form>
  {% endif %}
  </div>
</div>
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


def _resolve_user_id_for_request():
    """See auth.py:resolve_user_id. Falls back to a fixed pseudo-id only in
    the local-dev, no-Easy-Auth path (itself opt-in via LOCAL_DEV_CLIENTS) --
    in Azure this always comes from the validated `oid` claim."""
    header_value = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    if header_value is None:
        return "local-dev" if _LOCAL_DEV_CLIENTS else None
    claims = parse_client_principal(header_value)
    return resolve_user_id(claims) or "unknown-user"


def _handle(conversation_id):
    error = None
    turns = []
    allowed_clients = _resolve_allowed_clients_for_request()
    user_id = _resolve_user_id_for_request()
    client_id = (
        request.form.get("client_id")
        or request.args.get("client_id")
        or (allowed_clients[0] if allowed_clients else "")
    )
    query = request.form.get("query", "")

    if conversation_id and client_id in allowed_clients and user_id:
        meta = _get_conversation_meta(client_id, user_id, conversation_id)
        if meta is None:
            return redirect(url_for("index", client_id=client_id))
        turns = _load_turns(conversation_id)

    if request.method == "POST" and query.strip():
        if client_id not in allowed_clients:
            # Never trust the posted value alone -- re-validated here against
            # THIS request's own resolved set, not a global list.
            error = f"Client inconnu ou non autorisé pour votre compte : {client_id}"
        else:
            try:
                load_engine_config(client_id)  # fail fast with a clear error if misconfigured
                token = get_search_bearer_token()

                # Design note (2026-09-24, Jalon 9 -- diagnostic screenshot
                # upload, layer 1): a screenshot attached to the question
                # routes to analyze_screenshot_query_core_keyless() instead
                # of answer_query_core_keyless() -- same retrieval/sources
                # contract (primary_source/related_sources), plus
                # screen_reading/detected_error_codes. Processed in memory
                # only: the image itself is never written to blob storage or
                # to the conversation table, only the model's own textual
                # reading of it -- no new retention/PII surface to manage.
                screenshot = request.files.get("screenshot")
                if screenshot and screenshot.filename:
                    image_b64 = base64.b64encode(screenshot.read()).decode("ascii")
                    image_mime = screenshot.mimetype or "image/png"
                    raw = analyze_screenshot_query_core_keyless(
                        client_id, query, image_b64, image_mime, token, _aoai_client
                    )
                else:
                    raw = answer_query_core_keyless(client_id, query, token, _aoai_client)

                primary = raw.get("primary_source")
                related = raw.get("related_sources", [])
                confidence_label, confidence_level = _confidence(
                    (raw.get("_trace") or {}).get("primary_reranker_score")
                )
                entry = {
                    "query": query,
                    "answer_html": _render_answer_html(raw.get("answer")),
                    "ambiguous": raw.get("ambiguous"),
                    "unanswerable_reason": raw.get("unanswerable_reason"),
                    "confidence_label": confidence_label,
                    "confidence_level": confidence_level,
                    "modality_summary": _modality_summary(primary, related),
                    "primary_source": primary,
                    "related_sources": related,
                    "screen_reading": raw.get("screen_reading"),
                    "detected_error_codes": raw.get("detected_error_codes"),
                }

                persisted_id = None
                if user_id:
                    try:
                        cid = conversation_id or _create_conversation(client_id, user_id, query)
                        turn_index = _append_turn(cid, entry)
                        _touch_conversation(client_id, user_id, cid, turn_index + 1)
                        persisted_id = cid
                    except Exception:
                        pass  # Table not reachable/authorized yet -- degrade below

                if persisted_id:
                    return redirect(url_for("conversation", conversation_id=persisted_id, client_id=client_id))

                # Persistence unavailable -- show the answer anyway, as a
                # one-off, unsaved turn (never block answering over this).
                turns = [entry]
                conversation_id = None
            except Exception as exc:  # surfaced to the user, not swallowed
                error = str(exc)

    conversations = _list_conversations(client_id, user_id) if user_id else []

    return render_template_string(
        PAGE,
        allowed_clients=allowed_clients,
        client_id=client_id,
        query=query if not turns else "",
        error=error,
        turns=turns,
        conversations=conversations,
        active_conversation_id=conversation_id,
        example_questions=EXAMPLE_QUESTIONS,
    )


@app.route("/", methods=["GET", "POST"])
def index():
    return _handle(conversation_id=None)


@app.route("/c/<conversation_id>", methods=["GET", "POST"])
def conversation(conversation_id):
    return _handle(conversation_id=conversation_id)


@app.route("/healthz")
def healthz():
    # Deliberately does not list client ids (unauthenticated endpoint) --
    # just proves the app started and loaded its access maps.
    onboarded = len(_TENANT_ONLY_MAP) + len(_TENANT_GROUP_MAP)
    return {"status": "ok", "onboarded_clients": onboarded}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
