# =====================================================================
# KnowledgeEngine v9 — Auth & isolation (Jalon 5)
#
# Two-level resolution, using ONLY claims Easy Auth itself attached to the
# request (never a value the browser posted):
#
#   1. Tenant (`tid` claim) — checked against every entraTenantId known
#      from engine.<client>.yaml configs. A tenant not found there is
#      denied outright, whatever groups the user has. This is what makes
#      an external organization's Entra tenant (once onboarded — its
#      entraTenantId added to its own engine.<client>.yaml) usable at
#      all: the App Registration is multi-tenant (any Entra tenant can
#      complete sign-in after its admin consents), so this allowlist
#      check is the REAL access boundary, not the Entra config itself.
#
#   2. Group (`groups` claim) — only meaningful when a tenant hosts
#      several clients (Yassine's own sandbox tenant today: clienta/b/c,
#      client-v/s all live there). A client config WITHOUT
#      `access.entraGroup` means "this whole tenant = this client" (an
#      external organization, one tenant = one client — decision
#      2026-09-12); a client config WITH `access.entraGroup` requires
#      that specific group.
#
# Deny-by-default throughout: an unrecognized tenant, or a recognized
# shared tenant with no matching group, resolves to an empty client list
# — never a fallback/default client. See project memory
# jalon5-auth-isolation.md for the full discussion.
# =====================================================================
import base64
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestration"))
from answer import CONFIG_DIRS, load_engine_config  # noqa: E402 -- reuse, not a duplicate (axiom A4)

# Easy Auth (App Service Authentication V2) normalizes AAD v2 token claims
# to short names in most cases, but the fully-qualified WS-Federation/SAML
# style URIs also show up depending on token version/config — check both.
_TENANT_CLAIM_TYPES = {"tid", "http://schemas.microsoft.com/identity/claims/tenantid"}
_GROUP_CLAIM_TYPES = {"groups", "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"}

# Known limitation (not handled here): Entra only emits `groups` inline
# below ~200 groups per user; beyond that it emits a "groups overage"
# claim instead and a Microsoft Graph call is required to fetch the real
# list. Unlikely on Yassine's sandbox tenant; documented in project memory
# rather than handled, for this first pass.

TenantOnlyMap = Dict[str, str]
TenantGroupMap = Dict[Tuple[str, str], str]


def _all_client_ids() -> List[str]:
    """Every client_id known to this deployment, tracked (config/) and
    git-ignored real ones (clients-local/) alike — same discovery order
    axiom A2 already relies on elsewhere (load_engine_config)."""
    ids = []
    for d in CONFIG_DIRS:
        if d.exists():
            for p in sorted(d.glob("engine.*.yaml")):
                ids.append(p.stem.split("engine.", 1)[1])
    return ids


def _is_placeholder(value: Optional[str]) -> bool:
    """A client not yet onboarded (entraTenantId) or not yet assigned a
    real Entra group (entraGroup) keeps the literal 'TODO-...' value from
    its template — such a config must never accidentally match a token."""
    return not value or value.startswith("TODO-")


def build_access_maps() -> Tuple[TenantOnlyMap, TenantGroupMap]:
    """Loads every engine.<client>.yaml once (called at app startup, not
    per-request) and splits them into:
      - tenant_only: {entraTenantId: client_id} — whole tenant = this
        client. Only for a config where `access.entraGroup` is genuinely
        ABSENT (an external organization onboarded as "their whole tenant
        = this one client" — decision 2026-09-12).
      - tenant_group: {(entraTenantId, entraGroup): client_id} — this
        client shares its tenant with others; entraGroup disambiguates.

    A config with entraTenantId still == 'TODO-...' is skipped (never
    onboarded). A config that HAS an entraGroup key but it still reads
    'TODO-...' is also skipped, not treated as tenant-only — the field is
    reserved precisely so a client isn't accidentally granted "whole
    tenant" access just because nobody has filled in its real group yet
    (this is the actual state of every client shipped in this repo today:
    all five need a real Entra group created + entraGroup filled in via
    the portal before they become reachable — see project memory
    jalon5-auth-isolation.md, "reste à faire").

    Raises if two clients would both claim the same tenant as tenant-only
    — that is always a configuration mistake (a tenant can have at most
    one "whole tenant = this client" owner; anything else must use
    entraGroup instead), never a state to resolve silently.
    """
    tenant_only: TenantOnlyMap = {}
    tenant_group: TenantGroupMap = {}

    for client_id in _all_client_ids():
        cfg = load_engine_config(client_id)
        access = cfg.get("access") or {}
        tenant_id = access.get("entraTenantId")

        if _is_placeholder(tenant_id):
            continue  # not onboarded yet -- must never match any token

        if "entraGroup" not in access:
            if tenant_id in tenant_only:
                raise ValueError(
                    f"Both '{tenant_only[tenant_id]}' and '{client_id}' claim "
                    f"tenant {tenant_id!r} as tenant-only (no entraGroup). At "
                    "most one client may own a whole tenant -- give the others "
                    "an access.entraGroup instead."
                )
            tenant_only[tenant_id] = client_id
            continue

        group_id = access.get("entraGroup")
        if _is_placeholder(group_id):
            continue  # entraGroup reserved but not filled in yet -- deny, don't guess

        tenant_group[(tenant_id, group_id)] = client_id

    return tenant_only, tenant_group


def parse_client_principal(header_value: Optional[str]) -> List[dict]:
    """Decodes the X-MS-CLIENT-PRINCIPAL header Easy Auth injects into
    every authenticated request once authsettingsV2 is enabled (works the
    same whether the App Registration is single- or multi-tenant) -- a
    base64 JSON object with a `claims` list. Returns [] if the header is
    absent or malformed rather than raising, so a request that somehow
    reaches this code without Easy Auth in front of it (local dev) is
    denied by resolve_allowed_clients(), not crashed."""
    if not header_value:
        return []
    try:
        decoded = base64.b64decode(header_value)
        principal = json.loads(decoded)
        return principal.get("claims", [])
    except Exception:
        return []


def _claim_values(claims: List[dict], claim_types: set) -> List[str]:
    return [c.get("val") for c in claims if c.get("typ") in claim_types and c.get("val")]


def resolve_allowed_clients(
    claims: List[dict],
    tenant_only: TenantOnlyMap,
    tenant_group: TenantGroupMap,
) -> List[str]:
    """The ONLY function that decides which client_id(s) an authenticated
    request may query. Deny-by-default: no tenant claim, an unrecognized
    tenant, or a recognized shared tenant with no matching group all
    resolve to [] -- never a fallback client. Never takes client_id from
    anything the browser posted; only from these claims, which Easy Auth
    itself attached after validating the Entra ID token."""
    tenant_ids = _claim_values(claims, _TENANT_CLAIM_TYPES)
    if not tenant_ids:
        return []
    tenant_id = tenant_ids[0]

    if tenant_id in tenant_only:
        return [tenant_only[tenant_id]]

    group_ids = _claim_values(claims, _GROUP_CLAIM_TYPES)
    allowed: List[str] = []
    for group_id in group_ids:
        client_id = tenant_group.get((tenant_id, group_id))
        if client_id and client_id not in allowed:
            allowed.append(client_id)
    return allowed
