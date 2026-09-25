# =====================================================================
# KnowledgeEngine v9 — Web interface (Jalon 4, auth updated Jalon 5)
#
# Thin Flask front end over orchestration/answer.py's
# diagnostic_query_core_keyless() (2026-09-24 later, layer 2 -- was
# answer_query_core_keyless()/analyze_screenshot_query_core_keyless() before
# the multi-turn state work): retrieval + A3 hierarchy split + Structured
# Outputs generation, largely unchanged from Jalon 3 -- this file adds
# nothing to the RAG pipeline itself, only a form and an HTTP entry point.
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
import html
import json
import os
import re
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
    build_aoai_client_keyless,
    diagnostic_query_core_keyless,
    load_engine_config,
)

from auth import (  # noqa: E402
    build_access_maps,
    parse_client_principal,
    resolve_allowed_clients,
    resolve_display_name,
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

# Jalon 10 (step 10.3): ITSM review tab -- separate module/blueprint, see app/itsm.py.
# Records agent decisions only; never executes anything on Entra ID / ServiceNow.
from itsm import create_itsm_blueprint, itsm_access_for_request  # noqa: E402

app.register_blueprint(create_itsm_blueprint(_table_service, _credential))

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


def _initials(name):
    parts = [p for p in re.split(r"[\s._@-]+", (name or "").strip()) if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


app.jinja_env.filters["initials"] = _initials
# clean_excerpt filter is registered right after _clean_excerpt is defined (below):
# registering it here raised NameError at import -> the site failed to start (2026-09-25).

# Allowlist for the sanitized answer HTML -- deliberately no attributes at
# all (no href/src/on*), see module docstring.
_MD_ALLOWED_TAGS = {"p", "strong", "em", "ul", "ol", "li", "br", "code", "pre", "blockquote", "h3", "h4"}


def _render_answer_html(text):
    rendered = _markdown_lib.markdown(text or "", extensions=["nl2br", "sane_lists"])
    return nh3.clean(rendered, tags=_MD_ALLOWED_TAGS, attributes={})


_EXCERPT_HTML_HINT_RE = re.compile(
    r"</?(?:table|tr|td|th|thead|tbody|p|div|br|li|ul|ol|h[1-6])\b", re.IGNORECASE
)
_EXCERPT_BLOCK_BREAK_RE = re.compile(
    r"</?(?:tr|td|th|table|thead|tbody|p|div|br|li|ul|ol|h[1-6])\b[^>]*>", re.IGNORECASE
)
_EXCERPT_ANY_TAG_RE = re.compile(r"<[^>]+>")


def _clean_excerpt(text):
    """See module note above _EXCERPT_HTML_HINT_RE's definition site."""
    if not text or not _EXCERPT_HTML_HINT_RE.search(text):
        return text
    spaced = _EXCERPT_BLOCK_BREAK_RE.sub("\n", text)
    stripped = _EXCERPT_ANY_TAG_RE.sub("", spaced)
    unescaped = html.unescape(stripped)
    lines = [ln.strip() for ln in unescaped.splitlines() if ln.strip()]
    return "\n".join(lines)


app.jinja_env.filters["clean_excerpt"] = _clean_excerpt


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
            "answer_text": entry.get("answer_text") or "",
            "screen_reading": entry.get("screen_reading") or "",
            "detected_error_codes_json": json.dumps(entry.get("detected_error_codes") or []),
            "prochaine_verification": entry.get("prochaine_verification") or "",
            "diagnostic_termine": bool(entry.get("diagnostic_termine")),
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
                "answer_text": e.get("answer_text") or "",
                "screen_reading": e.get("screen_reading") or None,
                "detected_error_codes": json.loads(e.get("detected_error_codes_json") or "[]"),
                "prochaine_verification": e.get("prochaine_verification") or None,
                "diagnostic_termine": bool(e.get("diagnostic_termine")),
            }
        )
    return turns


def _delete_conversation(client_id: str, user_id: str, conv_id: str) -> bool:
    """Deletes a conversation's index entry and all of its turn rows. Same
    ownership check as _get_conversation_meta -- a conversation that
    doesn't exist or belongs to someone else is silently a no-op, never a
    distinct error either way."""
    if _get_conversation_meta(client_id, user_id, conv_id) is None:
        return False
    try:
        for row in _conv_turns_client.query_entities(
            query_filter="PartitionKey eq @pk", parameters={"pk": conv_id}, select=["RowKey"]
        ):
            _conv_turns_client.delete_entity(partition_key=conv_id, row_key=row["RowKey"])
        _conv_index_client.delete_entity(
            partition_key=_user_partition(client_id, user_id), row_key=conv_id
        )
        return True
    except Exception:
        return False


PAGE = """
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KnowledgeEngine v9 — Démo</title>
<style>
  :root{
    --bg:#f7f4ef; --panel:#ffffff; --panel-2:#faf3ea; --line:#e8ddd0;
    --txt:#20211f; --muted:#7a7267; --accent:#e2703a; --accent-soft:#eda374;
    --accent-tint:#fbe9dc; --accent-blue:#5c85cf; --accent-blue-tint:#e9f0fb;
    --ok:#1f9d76; --ok-tint:#e3f5ee; --warn:#8a6a3f; --warn-tint:#fdf0c6;
    --danger:#b3372a; --danger-tint:#fbe4e0; --btn-bg:#14172a; --btn-text:#ffffff;
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
    box-shadow:0 0 0 3px var(--accent-tint)}
  header h1{margin:0;font-size:1.02rem;font-weight:650;letter-spacing:.01em}
  header p{margin:2px 0 0;color:var(--muted);font-size:.78rem}
  .profile-menu{position:relative;flex-shrink:0}
  .profile-avatar{width:34px;height:34px;border-radius:50%;border:0;cursor:pointer;
    background:var(--btn-bg);color:var(--btn-text);font-size:.76rem;font-weight:650;
    letter-spacing:.02em;display:flex;align-items:center;justify-content:center;
    font-family:inherit;transition:opacity .15s}
  .profile-avatar:hover{opacity:.85}
  .profile-dropdown{position:absolute;top:calc(100% + 8px);right:0;min-width:190px;
    background:var(--panel);border:1px solid var(--line);border-radius:10px;
    box-shadow:0 10px 28px rgba(32,25,15,.16);padding:8px;z-index:40}
  .profile-dropdown[hidden]{display:none}
  .profile-dropdown-name{font-size:.82rem;color:var(--txt);font-weight:600;
    padding:5px 8px 9px;border-bottom:1px solid var(--line);margin-bottom:6px;
    overflow-wrap:anywhere}
  .profile-dropdown-logout{display:block;padding:8px;border-radius:7px;
    color:var(--danger);text-decoration:none;font-size:.82rem;transition:background .15s}
  .profile-dropdown-logout:hover{background:var(--danger-tint)}
  .layout{display:flex;flex:1;min-height:0;overflow:hidden}
  .sidebar{width:250px;flex-shrink:0;padding:16px 10px;border-right:1px solid var(--line);
    background:var(--panel);overflow-y:auto;display:flex;flex-direction:column}
  .sidebar-new{display:flex;align-items:center;justify-content:center;gap:6px;
    background:var(--btn-bg);color:var(--btn-text);font-weight:650;text-decoration:none;
    border-radius:8px;padding:10px 12px;font-size:.85rem;margin-bottom:12px;
    transition:opacity .15s;flex-shrink:0}
  .sidebar-new:hover{opacity:.88}
  .sidebar-section-label{font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;
    color:var(--muted);padding:4px 10px 6px}
  .conv-list{display:flex;flex-direction:column;gap:2px}
  .conv-row{display:flex;align-items:center;gap:2px}
  .conv-link{flex:1;min-width:0;display:block;padding:9px 10px;border-radius:8px;color:var(--muted);
    text-decoration:none;border-left:2px solid transparent;transition:background .15s,color .15s}
  .conv-link-title{font-size:.82rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    color:inherit;font-weight:500}
  .conv-link-meta{font-size:.7rem;color:var(--muted);margin-top:1px;opacity:.8}
  .conv-link:hover{background:var(--panel-2);color:var(--txt)}
  .conv-link.active{background:var(--panel-2);color:var(--txt);border-left-color:var(--accent)}
  .conv-delete-form{margin:0;flex-shrink:0}
  .conv-delete-btn{background:none;border:0;cursor:pointer;font-size:.8rem;line-height:1;
    padding:7px 8px;border-radius:6px;color:var(--muted);opacity:.5;
    transition:opacity .15s,color .15s,background .15s}
  .conv-row:hover .conv-delete-btn{opacity:.85}
  .conv-delete-btn:hover{opacity:1;color:var(--danger);background:var(--danger-tint)}
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
  .denied{padding:16px;border:1px solid var(--danger);background:var(--danger-tint);
    border-radius:10px;color:var(--danger);font-size:.9rem;max-width:760px;margin:0 auto}
  .error{padding:12px 14px;border:1px solid var(--danger);background:var(--danger-tint);
    border-radius:8px;color:var(--danger);font-size:.88rem;margin-bottom:16px}
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
    border-radius:12px;border-left:3px solid var(--accent-soft)}
  .turn-a.ambiguous{border-left-color:var(--warn)}
  .turn-a-label{font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;
    color:var(--accent);margin-bottom:8px;font-weight:650}
  .answer-text p{margin:0 0 10px}
  .answer-text p:last-child{margin-bottom:0}
  .answer-text ul,.answer-text ol{margin:6px 0 10px;padding-left:22px}
  .answer-text li{margin:3px 0}
  .answer-text code{background:var(--panel-2);padding:1px 5px;border-radius:4px;font-size:.85em}
  .ambiguous-note{margin-top:10px;padding:8px 10px;border-radius:7px;background:var(--warn-tint);
    color:var(--warn);font-size:.83rem}
  .meta-row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:14px;font-size:.8rem}
  .confidence{display:inline-flex;align-items:center;gap:5px;padding:3px 11px;border-radius:999px;
    font-weight:600;font-size:.74rem;white-space:nowrap}
  .confidence .dot{width:6px;height:6px;border-radius:50%;display:inline-block}
  .confidence-high{background:var(--ok-tint);color:var(--ok)}
  .confidence-high .dot{background:var(--ok)}
  .confidence-medium{background:var(--warn-tint);color:var(--warn)}
  .confidence-medium .dot{background:var(--warn)}
  .confidence-low{background:var(--danger-tint);color:var(--danger)}
  .confidence-low .dot{background:var(--danger)}
  .confidence-unknown{background:var(--panel-2);color:var(--muted)}
  .confidence-unknown .dot{background:var(--muted)}
  .modality-summary{color:var(--muted)}
  .sources{margin-top:16px;display:flex;flex-direction:column;gap:12px}
  .source-group-label{display:flex;align-items:center;gap:6px;font-size:.72rem;
    text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin-bottom:6px}
  .glabel-icon{font-size:.9rem}
  .group-primary .source-group-label{color:var(--accent-blue)}
  .group-media-annex .source-group-label{color:var(--muted)}
  .src-card{padding:9px 11px;border:1px solid var(--line);border-radius:9px;margin-top:6px;
    font-size:.85rem;background:var(--panel-2)}
  .src-card:first-child{margin-top:0}
  .src-card.primary{border-color:var(--accent);background:var(--accent-tint)}
  .src-card.secondary{opacity:.85}
  .src-card-title{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
  .src-title{flex:1;min-width:0}
  .src-card.used .src-title{color:var(--ok)}
  .tag.used-tag{display:inline-block;font-size:.68rem;padding:1px 7px;border-radius:999px;
    background:var(--ok-tint);color:var(--ok)}
  .excerpt{margin-top:7px}
  .excerpt summary{cursor:pointer;font-size:.8rem;color:var(--accent);list-style:none}
  .excerpt summary::-webkit-details-marker{display:none}
  .excerpt summary:hover{opacity:.85}
  .excerpt-hidden{margin-top:7px;font-size:.78rem;color:var(--muted);font-style:italic;
    display:flex;align-items:center;gap:5px}
  .safe-summary{margin-top:7px;padding:9px 11px;border-radius:8px;background:var(--accent-tint);
    border:1px solid var(--accent-soft);color:var(--txt);font-size:.83rem;line-height:1.5}
  .excerpt-text{white-space:pre-wrap;word-break:break-word;margin:7px 0 0;padding:10px 12px;
    background:var(--bg);border:1px solid var(--line);border-radius:7px;font-size:.85rem;
    color:var(--txt);max-height:240px;overflow:auto;font-family:inherit;line-height:1.55}
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
  .send-btn{flex-shrink:0;background:var(--btn-bg);color:var(--btn-text);border:none;border-radius:10px;
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
  .next-check-note{margin:10px 0 0;font-size:.82rem;color:var(--accent-blue);
    background:var(--accent-blue-tint);border-radius:8px;padding:8px 10px}
  .diagnostic-done-badge{margin:10px 0 0;font-size:.82rem;color:var(--ok);
    background:var(--ok-tint);border:1px solid var(--ok);border-radius:8px;padding:6px 10px;display:inline-block}
  .modal-overlay{position:fixed;inset:0;background:rgba(32,28,20,.45);
    display:flex;align-items:center;justify-content:center;z-index:50;padding:16px}
  .modal-overlay[hidden]{display:none}
  .modal-card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:20px 22px;max-width:340px;width:100%;box-shadow:0 16px 40px rgba(32,25,15,.2)}
  .modal-title{font-size:.96rem;font-weight:650;color:var(--txt)}
  .modal-sub{margin-top:6px;font-size:.82rem;color:var(--muted);line-height:1.5}
  .modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
  .modal-btn{border:0;border-radius:9px;padding:8px 16px;font-size:.84rem;font-weight:600;
    cursor:pointer;transition:opacity .15s;font-family:inherit}
  .modal-btn-cancel{background:var(--panel-2);color:var(--txt);border:1px solid var(--line)}
  .modal-btn-cancel:hover{opacity:.8}
  .modal-btn-danger{background:var(--danger);color:#fff}
  .modal-btn-danger:hover{opacity:.88}
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
  {% if itsm_enabled %}<a href="/itsm" style="margin-left:auto;margin-right:14px;color:var(--txt);font-size:.85rem;font-weight:600;text-decoration:none">Tickets ITSM →</a>{% endif %}
  <div class="profile-menu">
    <button type="button" class="profile-avatar" id="profile-avatar-btn"
      title="{{ display_name }}" aria-haspopup="true" aria-expanded="false">{{ display_name | initials }}</button>
    <div class="profile-dropdown" id="profile-dropdown" hidden>
      <div class="profile-dropdown-name">{{ display_name }}</div>
      <a class="profile-dropdown-logout" href="/.auth/logout?post_logout_redirect_uri=/">Se déconnecter</a>
    </div>
  </div>
</header>
<div class="layout">
  <aside class="sidebar">
    <a class="sidebar-new" href="/?client_id={{ client_id }}">＋ Nouvelle conversation</a>
    <div class="sidebar-section-label">Conversations</div>
    <div class="conv-list">
      {% for c in conversations %}
      <div class="conv-row">
        <a class="conv-link {% if c.RowKey == active_conversation_id %}active{% endif %}"
           href="/c/{{ c.RowKey }}?client_id={{ client_id }}">
          <div class="conv-link-title">{{ c.title }}</div>
          <div class="conv-link-meta">{{ c.turnCount }} échange{{ "s" if c.turnCount and c.turnCount > 1 else "" }}</div>
        </a>
        <form method="post" action="/c/{{ c.RowKey }}/delete" class="conv-delete-form">
          <input type="hidden" name="client_id" value="{{ client_id }}">
          <button type="submit" class="conv-delete-btn" title="Supprimer la conversation">🗑</button>
        </form>
      </div>
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
        {% if t.diagnostic_termine %}
        <div class="diagnostic-done-badge">✅ Diagnostic considéré comme terminé</div>
        {% elif t.prochaine_verification %}
        <div class="next-check-note">🔎 Prochaine vérification : {{ t.prochaine_verification }}</div>
        {% endif %}

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
                <div class="excerpt-text">{{ t.primary_source.excerpt | clean_excerpt }}</div>
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
                <div class="excerpt-text">{{ s.excerpt | clean_excerpt }}</div>
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
        <select name="client_id" class="client-select" title="Client"
          onchange="document.querySelectorAll('.examples input[name=client_id]').forEach(i=>i.value=this.value)">
          {% for c in allowed_clients %}
          <option value="{{ c }}" {% if c == client_id %}selected{% endif %}>{{ c }}</option>
          {% endfor %}
        </select>
        <textarea name="query" rows="2" placeholder="Pose ta question... (ou joins juste une capture)">{{ query }}</textarea>
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
<div id="confirm-modal" class="modal-overlay" hidden>
  <div class="modal-card">
    <div class="modal-title">Supprimer cette conversation ?</div>
    <div class="modal-sub">Cette action est définitive : la conversation et tous ses échanges seront supprimés.</div>
    <div class="modal-actions">
      <button type="button" class="modal-btn modal-btn-cancel">Annuler</button>
      <button type="button" class="modal-btn modal-btn-danger">Supprimer</button>
    </div>
  </div>
</div>
<script>
(function(){
  var turns = document.querySelectorAll('.turn');
  var lastTurn = turns[turns.length - 1];
  if (lastTurn) { lastTurn.scrollIntoView({block: 'start'}); }

  var form = document.querySelector('form.composer');
  var textarea = form ? form.querySelector('textarea[name=query]') : null;
  var fileInput = form ? form.querySelector('input[name=screenshot]') : null;
  function hasContent(){
    return !!(textarea && textarea.value.trim()) || !!(fileInput && fileInput.files.length);
  }
  if (form && textarea) {
    // Entree envoie (Maj+Entree = saut de ligne) ; une capture seule, sans
    // texte, est desormais un envoi valide.
    textarea.addEventListener('keydown', function(e){
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (hasContent()) { form.requestSubmit ? form.requestSubmit() : form.submit(); }
      }
    });
    form.addEventListener('submit', function(e){
      if (!hasContent()) { e.preventDefault(); }
    });
  }

  // Profile avatar dropdown (name + "Se deconnecter"), replacing the
  // plain logout link.
  var avatarBtn = document.getElementById('profile-avatar-btn');
  var profileDropdown = document.getElementById('profile-dropdown');
  if (avatarBtn && profileDropdown) {
    avatarBtn.addEventListener('click', function(e){
      e.stopPropagation();
      var willOpen = profileDropdown.hidden;
      profileDropdown.hidden = !willOpen;
      avatarBtn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    });
    document.addEventListener('click', function(e){
      if (!profileDropdown.hidden && !profileDropdown.contains(e.target) && e.target !== avatarBtn) {
        profileDropdown.hidden = true;
        avatarBtn.setAttribute('aria-expanded', 'false');
      }
    });
    document.addEventListener('keydown', function(e){
      if (e.key === 'Escape' && !profileDropdown.hidden) {
        profileDropdown.hidden = true;
        avatarBtn.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // Themed confirm modal for "supprimer la conversation", replacing the
  // browser's native confirm() dialog.
  var modal = document.getElementById('confirm-modal');
  var modalCancel = modal ? modal.querySelector('.modal-btn-cancel') : null;
  var modalConfirm = modal ? modal.querySelector('.modal-btn-danger') : null;
  var pendingDeleteForm = null;
  function closeModal(){
    if (modal) { modal.hidden = true; }
    pendingDeleteForm = null;
  }
  document.querySelectorAll('.conv-delete-form').forEach(function(delForm){
    delForm.addEventListener('submit', function(e){
      e.preventDefault();
      pendingDeleteForm = delForm;
      if (modal) { modal.hidden = false; }
    });
  });
  if (modal) {
    modalCancel.addEventListener('click', closeModal);
    modalConfirm.addEventListener('click', function(){
      var f = pendingDeleteForm;
      closeModal();
      if (f) { f.submit(); }
    });
    modal.addEventListener('click', function(e){ if (e.target === modal) { closeModal(); } });
    document.addEventListener('keydown', function(e){
      if (e.key === 'Escape' && !modal.hidden) { closeModal(); }
    });
  }
})();
</script>
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


def _resolve_display_name_for_request():
    """See auth.py:resolve_display_name. Header's profile avatar/dropdown
    only -- never part of an access decision."""
    header_value = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    if header_value is None:
        return "Démo locale" if _LOCAL_DEV_CLIENTS else None
    claims = parse_client_principal(header_value)
    return resolve_display_name(claims) or "Utilisateur"


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
    # Detected up front (2026-09-... "capture seule, sans texte" UI
    # request): the file itself is only read further down, once we know
    # we're actually processing this turn.
    screenshot = request.files.get("screenshot") if request.method == "POST" else None
    has_screenshot = bool(screenshot and screenshot.filename)

    if conversation_id and client_id in allowed_clients and user_id:
        meta = _get_conversation_meta(client_id, user_id, conversation_id)
        if meta is None:
            return redirect(url_for("index", client_id=client_id))
        turns = _load_turns(conversation_id)

    if request.method == "POST" and (query.strip() or has_screenshot):
        if not query.strip():
            query = "Que montre cette capture d'écran ?"
        if client_id not in allowed_clients:
            # Never trust the posted value alone -- re-validated here against
            # THIS request's own resolved set, not a global list.
            error = f"Client inconnu ou non autorisé pour votre compte : {client_id}"
        else:
            try:
                load_engine_config(client_id)  # fail fast with a clear error if misconfigured
                token = get_search_bearer_token()

                # Design note (2026-09-24, later same day -- diagnostic
                # layer 2: persistent multi-turn state). Every turn of a
                # conversation now goes through diagnostic_query_core_keyless
                # -- a screenshot attached THIS turn is still optional
                # (image_b64=None otherwise), but the call is the same one
                # whether or not an image is attached and whether or not
                # this is the first turn: `turns` (loaded above from
                # _load_turns when conversation_id was given, [] for a new
                # conversation) is passed as prior_turns so the model gets
                # the deterministic ETAT DU DIAGNOSTIC block built from
                # everything already established in this conversation
                # (fiches servies, codes d'erreur, entites, tours precedents)
                # -- see orchestration/answer.py's design note above
                # DIAGNOSTIC_SYSTEM_PROMPT. Replaces the separate
                # answer_query_core_keyless / analyze_screenshot_query_core_keyless
                # branching from layer 1. Screenshot handling is unchanged:
                # processed in memory only, never written to blob storage or
                # to the conversation table, only the model's own textual
                # reading of it.
                image_b64 = image_mime = None
                if has_screenshot:
                    image_b64 = base64.b64encode(screenshot.read()).decode("ascii")
                    image_mime = screenshot.mimetype or "image/png"

                raw = diagnostic_query_core_keyless(
                    client_id,
                    query,
                    turns,
                    token,
                    _aoai_client,
                    image_b64=image_b64,
                    image_mime=image_mime,
                )

                primary = raw.get("primary_source")
                related = raw.get("related_sources", [])
                confidence_label, confidence_level = _confidence(
                    (raw.get("_trace") or {}).get("primary_reranker_score")
                )
                entry = {
                    "query": query,
                    "answer_text": raw.get("answer") or "",
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
                    "prochaine_verification": raw.get("prochaine_verification"),
                    "diagnostic_termine": raw.get("diagnostic_termine"),
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
        display_name=_resolve_display_name_for_request(),
        client_id=client_id,
        query=query if not turns else "",
        error=error,
        turns=turns,
        conversations=conversations,
        active_conversation_id=conversation_id,
        example_questions=EXAMPLE_QUESTIONS,
        itsm_enabled=itsm_access_for_request(),
    )


@app.route("/", methods=["GET", "POST"])
def index():
    return _handle(conversation_id=None)


@app.route("/c/<conversation_id>", methods=["GET", "POST"])
def conversation(conversation_id):
    return _handle(conversation_id=conversation_id)


@app.route("/c/<conversation_id>/delete", methods=["POST"])
def delete_conversation(conversation_id):
    allowed_clients = _resolve_allowed_clients_for_request()
    user_id = _resolve_user_id_for_request()
    client_id = request.form.get("client_id") or (allowed_clients[0] if allowed_clients else "")
    if client_id in allowed_clients and user_id:
        _delete_conversation(client_id, user_id, conversation_id)
    return redirect(url_for("index", client_id=client_id))


@app.route("/healthz")
def healthz():
    # Deliberately does not list client ids (unauthenticated endpoint) --
    # just proves the app started and loaded its access maps.
    onboarded = len(_TENANT_ONLY_MAP) + len(_TENANT_GROUP_MAP)
    return {"status": "ok", "onboarded_clients": onboarded}, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
