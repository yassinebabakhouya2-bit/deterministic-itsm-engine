# Generates workflow-definition.json for the ITSM proposal Logic App (Jalon 10, step 10.2).
# Kept as a generator because Logic App expressions are much easier to review as Python strings
# than as hand-escaped JSON. Re-run after editing: python itsm/propose/gen_workflow.py
import json, os

GRAPH = "https://graph.microsoft.com"
MI_GRAPH = {"type": "ManagedServiceIdentity", "audience": GRAPH}
MI_STORAGE = {"type": "ManagedServiceIdentity", "audience": "https://storage.azure.com"}
MI_AOAI = {"type": "ManagedServiceIdentity", "audience": "https://cognitiveservices.azure.com"}
TABLE_HEADERS = {"x-ms-version": "2020-12-06", "Accept": "application/json;odata=nometadata"}
IT = "items('For_each_ticket')"
P = "outputs('Compose_proposal')"
A = P + "?['action']"
SECURITY_REASONS = ["target_privileged_role", "target_is_not_requester", "secret_requested_for_third_party",
                    "requester_not_manager", "group_not_allowlisted"]

SYSTEM_PROMPT = """You are the triage component of an IT service desk automation module.
You read ONE ticket (usually written in French) plus directory facts, and choose exactly ONE action from a closed list.
You never execute anything: a deterministic guard layer and then a human agent review your output.

Actions:
- mfa_reset: the account owner lost access to their MFA method (new/lost phone, Authenticator gone).
- password_reset: the account owner forgot their password or is locked out.
- group_add: an access request that maps to exactly one entry of allowed_groups.
- license_assign: a request for a software licence.
- offboarding: an employee is leaving and their access must be removed.
- escalate: anything else, ambiguous tickets, or several unrelated requests.

Fields:
- target_name: full name of the person whose account the action applies to, as written in the ticket; if the ticket is about the requester themself, use subject.display_name.
- target_is_requester: true ONLY if the account to act on belongs to the person who wrote the ticket (requester). A request made on behalf of someone else is false.
- secret_to_third_party: true if the ticket asks that a password, code or credential be sent to anyone other than the account owner.
- group_name: for group_add, the exact name from allowed_groups; otherwise "".
- license_sku_hint: for license_assign, the product keyword in upper case (e.g. VISIO, PROJECT); otherwise "".
- offboarding_steps: for offboarding, the steps the ticket asks for, from the allowed values; otherwise [].
- confidence: 0 to 1.
- rationale_fr: one or two sentences in French for the human agent explaining the choice. Never include any secret.

Report facts faithfully. Do not decide whether the action is allowed: the guard layer does that from your fields."""

SCHEMA = {
    "name": "itsm_proposal", "strict": True,
    "schema": {
        "type": "object", "additionalProperties": False,
        "required": ["action", "target_name", "target_is_requester", "secret_to_third_party", "group_name",
                     "license_sku_hint", "offboarding_steps", "confidence", "rationale_fr"],
        "properties": {
            "action": {"type": "string", "enum": ["mfa_reset", "password_reset", "group_add", "license_assign", "offboarding", "escalate"]},
            "target_name": {"type": "string"},
            "target_is_requester": {"type": "boolean"},
            "secret_to_third_party": {"type": "boolean"},
            "group_name": {"type": "string"},
            "license_sku_hint": {"type": "string"},
            "offboarding_steps": {"type": "array", "items": {"type": "string", "enum": ["disable_account", "revoke_sessions", "remove_groups", "remove_licenses"]}},
            "confidence": {"type": "number"},
            "rationale_fr": {"type": "string"},
        },
    },
}

def http(method, uri, auth, run_after, queries=None, headers=None, body=None, secure=False):
    a = {"type": "Http", "inputs": {"method": method, "uri": uri, "authentication": auth}, "runAfter": run_after}
    if queries: a["inputs"]["queries"] = queries
    if headers: a["inputs"]["headers"] = headers
    if body is not None: a["inputs"]["body"] = body
    return a

def ok(*names): return {n: ["Succeeded"] for n in names}

# NB: createArray() with no argument is INVALID in Logic Apps (InvalidTemplate at run time,
# hit 2026-09-25) -> use json('[]') for an empty array.
reasons = [
    f"if(and(or(equals({A},'password_reset'),equals({A},'mfa_reset')),greater(length(body('Filter_roles')),0)),createArray('target_privileged_role'),json('[]'))",
    f"if(and(or(equals({A},'password_reset'),equals({A},'mfa_reset')),equals({P}?['target_is_requester'],false)),createArray('target_is_not_requester'),json('[]'))",
    f"if(equals({P}?['secret_to_third_party'],true),createArray('secret_requested_for_third_party'),json('[]'))",
    f"if(and(equals({A},'offboarding'),not(equals(toLower(coalesce({IT}?['openedByEmail'],'')),toLower(outputs('Compose_manager_email'))))),createArray('requester_not_manager'),json('[]'))",
    f"if(and(equals({A},'group_add'),not(contains(parameters('allowedGroupNames'),{P}?['group_name']))),createArray('group_not_allowlisted'),json('[]'))",
    f"if(and(equals({A},'group_add'),contains(body('Select_group_names'),{P}?['group_name'])),createArray('already_member'),json('[]'))",
    f"if(and(equals({A},'license_assign'),equals(length(body('Filter_free_skus')),0)),createArray('no_free_license_seat'),json('[]'))",
    f"if(equals({A},'escalate'),createArray('llm_escalated'),json('[]'))",
    f"if(less({P}?['confidence'],0.6),createArray('low_confidence'),json('[]'))",
]
reasons_expr = "@union(" + ",".join(reasons) + ")"
sec = ",".join(f"'{r}'" for r in SECURITY_REASONS)
status_expr = (f"@if(greater(length(intersection(outputs('Compose_reasons'),createArray({sec}))),0),'refused',"
               f"if(greater(length(outputs('Compose_reasons')),0),'needs_human','pending_review'))")

row_uri = ("@{concat('https://', parameters('storageAccountName'), '.table.core.windows.net/', parameters('tableName'), "
           "'(PartitionKey=''', parameters('clientCode'), ''',RowKey=''', " + IT + "?['RowKey'], ''')')}")
merge_headers = dict(TABLE_HEADERS, **{"X-HTTP-Method": "MERGE", "Content-Type": "application/json"})

found_actions = {
    "Get_subject_memberOf": http("GET", f"@{{concat('{GRAPH}/v1.0/users/', body('Get_subject_user')?['id'], '/memberOf')}}", MI_GRAPH, {},
                                 queries={"$select": "id,displayName"}),
    "Get_subject_manager": http("GET", f"@{{concat('{GRAPH}/v1.0/users/', body('Get_subject_user')?['id'], '/manager')}}", MI_GRAPH,
                                ok("Get_subject_memberOf"), queries={"$select": "mail,userPrincipalName"}),
    "Get_subject_licenses": http("GET", f"@{{concat('{GRAPH}/v1.0/users/', body('Get_subject_user')?['id'], '/licenseDetails')}}", MI_GRAPH,
                                 {"Get_subject_manager": ["Succeeded", "Failed"]}, queries={"$select": "skuPartNumber"}),
    "Compose_manager_email": {"type": "Compose", "runAfter": ok("Get_subject_licenses"),
        "inputs": "@if(equals(outputs('Get_subject_manager')?['statusCode'],200),coalesce(body('Get_subject_manager')?['mail'],body('Get_subject_manager')?['userPrincipalName'],''),'')"},
    "Filter_roles": {"type": "Query", "runAfter": ok("Compose_manager_email"),
        "inputs": {"from": "@body('Get_subject_memberOf')?['value']", "where": "@equals(item()?['@odata.type'],'#microsoft.graph.directoryRole')"}},
    "Filter_groups": {"type": "Query", "runAfter": ok("Filter_roles"),
        "inputs": {"from": "@body('Get_subject_memberOf')?['value']", "where": "@equals(item()?['@odata.type'],'#microsoft.graph.group')"}},
    "Select_group_names": {"type": "Select", "runAfter": ok("Filter_groups"),
        "inputs": {"from": "@body('Filter_groups')", "select": "@item()?['displayName']"}},
    "Select_role_names": {"type": "Select", "runAfter": ok("Select_group_names"),
        "inputs": {"from": "@body('Filter_roles')", "select": "@item()?['displayName']"}},
    "Select_license_names": {"type": "Select", "runAfter": ok("Select_role_names"),
        "inputs": {"from": "@body('Get_subject_licenses')?['value']", "select": "@item()?['skuPartNumber']"}},
    "Compose_llm_input": {"type": "Compose", "runAfter": ok("Select_license_names"), "inputs": {
        "ticket_number": f"@{IT}?['RowKey']",
        "ticket_type": f"@{IT}?['ticketType']",
        "short_description": f"@{IT}?['shortDescription']",
        "description": f"@{IT}?['description']",
        "requester": {"username": f"@{IT}?['openedByUserName']", "email": f"@{IT}?['openedByEmail']"},
        "subject": {"username": f"@{IT}?['subjectUserName']", "email": f"@{IT}?['subjectEmail']",
                    "display_name": "@body('Get_subject_user')?['displayName']"},
        "subject_groups": "@body('Select_group_names')",
        "subject_licenses": "@body('Select_license_names')",
        "allowed_groups": "@parameters('allowedGroups')"}},
    "Call_llm": http("POST", "@{concat(parameters('aoaiEndpoint'), '/openai/deployments/', parameters('aoaiDeployment'), '/chat/completions')}",
                     MI_AOAI, ok("Compose_llm_input"), queries={"api-version": "@parameters('aoaiApiVersion')"},
                     headers={"Content-Type": "application/json"},
                     body={"temperature": 0, "max_tokens": 800,
                           "response_format": {"type": "json_schema", "json_schema": SCHEMA},
                           "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                        {"role": "user", "content": "@{string(outputs('Compose_llm_input'))}"}]}),
    "Compose_proposal": {"type": "Compose", "runAfter": ok("Call_llm"),
        "inputs": "@json(body('Call_llm')?['choices'][0]?['message']?['content'])"},
    "Filter_free_skus": {"type": "Query", "runAfter": ok("Compose_proposal"), "inputs": {
        "from": "@body('Get_subscribed_skus')?['value']",
        "where": f"@and(contains(toUpper(item()?['skuPartNumber']),if(empty({P}?['license_sku_hint']),'#NONE#',toUpper({P}?['license_sku_hint']))),greater(item()?['prepaidUnits']?['enabled'],item()?['consumedUnits']))"}},
    "Compose_reasons": {"type": "Compose", "runAfter": ok("Filter_free_skus"), "inputs": reasons_expr},
    "Compose_status": {"type": "Compose", "runAfter": ok("Compose_reasons"), "inputs": status_expr},
    "Merge_proposal": http("POST", row_uri, MI_STORAGE, ok("Compose_status"), headers=merge_headers, body={
        "proposalStatus": "@{outputs('Compose_status')}",
        "proposalAction": f"@{{{A}}}",
        "proposalJson": f"@{{string({P})}}",
        "proposalRationale": f"@{{{P}?['rationale_fr']}}",
        "proposalConfidence": f"@{{{P}?['confidence']}}",
        "guardReasons": "@{join(outputs('Compose_reasons'), ',')}",
        "subjectEntraId": "@{body('Get_subject_user')?['id']}",
        "subjectPrivilegedRoles": "@{join(body('Select_role_names'), ',')}",
        "subjectGroups": "@{join(body('Select_group_names'), ',')}",
        "managerEmail": "@{outputs('Compose_manager_email')}",
        "proposalModel": "@{parameters('aoaiDeployment')}",
        "proposedAtUtc": "@{utcNow()}"}),
}

not_found_actions = {
    "Merge_not_found": http("POST", row_uri, MI_STORAGE, {}, headers=merge_headers, body={
        "proposalStatus": "needs_human",
        "guardReasons": "subject_not_found_in_entra",
        "proposalRationale": "Utilisateur concerne introuvable dans Entra ID : traitement manuel requis.",
        "proposedAtUtc": "@{utcNow()}"}),
}

wf = {
    "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {
        "clientCode": {"type": "string"},
        "storageAccountName": {"type": "string"},
        "tableName": {"type": "string"},
        "aoaiEndpoint": {"type": "string"},
        "aoaiDeployment": {"type": "string"},
        "aoaiApiVersion": {"type": "string"},
        "allowedGroups": {"type": "array"},
        "allowedGroupNames": {"type": "array"},
    },
    # Trigger concurrency 1: never two runs at once (Enable + manual Run started two overlapping runs
    # on 2026-09-25 -> the same tickets were classified twice, last MERGE wins).
    "triggers": {"Recurrence": {"type": "Recurrence", "recurrence": {"frequency": "Minute", "interval": 5},
                                "runtimeConfiguration": {"concurrency": {"runs": 1}}}},
    "actions": {
        "Get_rows": http("GET", "@{concat('https://', parameters('storageAccountName'), '.table.core.windows.net/', parameters('tableName'), '()')}",
                         MI_STORAGE, {}, queries={"$filter": "@{concat('PartitionKey eq ''', parameters('clientCode'), '''')}"}, headers=TABLE_HEADERS),
        "Filter_unproposed": {"type": "Query", "runAfter": ok("Get_rows"),
            "inputs": {"from": "@body('Get_rows')?['value']", "where": "@empty(item()?['proposalStatus'])"}},
        "Get_subscribed_skus": http("GET", f"{GRAPH}/v1.0/subscribedSkus", MI_GRAPH, ok("Filter_unproposed"),
                                    queries={"$select": "skuPartNumber,prepaidUnits,consumedUnits"}),
        "For_each_ticket": {
            "type": "Foreach", "foreach": "@body('Filter_unproposed')", "runAfter": ok("Get_subscribed_skus"),
            "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
            "actions": {
                "Get_subject_user": http("GET", f"@{{concat('{GRAPH}/v1.0/users/', encodeUriComponent(coalesce({IT}?['subjectEmail'], 'unknown')))}}",
                                         MI_GRAPH, {}, queries={"$select": "id,displayName,userPrincipalName,accountEnabled"}),
                "If_subject_found": {
                    "type": "If", "runAfter": {"Get_subject_user": ["Succeeded", "Failed"]},
                    "expression": {"and": [{"equals": ["@outputs('Get_subject_user')?['statusCode']", 200]}]},
                    "actions": found_actions,
                    "else": {"actions": not_found_actions},
                },
            },
        },
    },
    "outputs": {},
}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow-definition.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(wf, f, indent=2, ensure_ascii=True)
print("written", out)
