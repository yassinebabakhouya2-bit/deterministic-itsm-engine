# Generates workflow-definition.json for the ITSM EXECUTOR Logic App (Jalon 10, step 10.4).
# Edit this file, then: python itsm/execute/gen_workflow.py  (and redeploy itsm/execute/main.bicep)
#
# Scope of this first executor (10.4a): group_add and offboarding (disable_account,
# revoke_sessions, remove_groups) + ServiceNow RITM closure. password_reset / mfa_reset
# produce a SECRET (temporary password / Temporary Access Pass) that must be delivered
# to the validating agent only -- that delivery channel is step 10.4b; until then those
# actions end as 'not_implemented' and nothing is executed. license_assign is always
# needs_human in the demo tenant (no free seat).
import json, os

GRAPH = "https://graph.microsoft.com"
MI_GRAPH = {"type": "ManagedServiceIdentity", "audience": GRAPH}
MI_STORAGE = {"type": "ManagedServiceIdentity", "audience": "https://storage.azure.com"}
IT = "items('For_each_ticket')"
P = "outputs('Compose_params')"
A = P + "?['action']"
UID = "body('Get_subject_user')?['id']"
IMPLEMENTED = ["group_add", "offboarding"]

def http(method, uri, auth, run_after, queries=None, headers=None, body=None, secure_inputs=False):
    a = {"type": "Http", "inputs": {"method": method, "uri": uri, "authentication": auth}, "runAfter": run_after}
    if queries: a["inputs"]["queries"] = queries
    if headers: a["inputs"]["headers"] = headers
    if body is not None: a["inputs"]["body"] = body
    if secure_inputs: a["runtimeConfiguration"] = {"secureData": {"properties": ["inputs"]}}
    return a

def ok(*n): return {x: ["Succeeded"] for x in n}
def done(*n): return {x: ["Succeeded", "Failed"] for x in n}

def log(name, run_after, step, ok_expr, detail_expr):
    return {name: {"type": "AppendToArrayVariable", "runAfter": run_after,
                   "inputs": {"name": "stepLog", "value": {"step": step, "ok": ok_expr, "detail": detail_expr}}}}

TABLE_HEADERS = {"x-ms-version": "2020-12-06", "Accept": "application/json;odata=minimalmetadata"}
row_uri = ("@{concat('https://', parameters('storageAccountName'), '.table.core.windows.net/', parameters('tableName'), "
           "'(PartitionKey=''', parameters('clientCode'), ''',RowKey=''', " + IT + "?['RowKey'], ''')')}")
merge_headers = {"x-ms-version": "2020-12-06", "Accept": "application/json;odata=nometadata",
                 "X-HTTP-Method": "MERGE", "Content-Type": "application/json"}

# ---------------------------------------------------------------- group_add
group_add = {}
group_add["Get_group"] = http("GET", f"{GRAPH}/v1.0/groups", MI_GRAPH, {},
    queries={"$filter": f"@{{concat('displayName eq ''', {P}?['group_name'], '''')}}", "$select": "id,displayName"})
group_add["Add_member"] = http("POST", f"@{{concat('{GRAPH}/v1.0/groups/', first(body('Get_group')?['value'])?['id'], '/members/$ref')}}",
    MI_GRAPH, ok("Get_group"), headers={"Content-Type": "application/json"},
    # JSON keys starting with '@' are parsed as expressions by Logic Apps -> escape as '@@'
    body={"@@odata.id": f"@{{concat('{GRAPH}/v1.0/directoryObjects/', {UID})}}"})
group_add.update(log("Log_group_add", {"Add_member": ["Succeeded", "Failed", "Skipped"]}, "group_add",
    "@or(equals(outputs('Add_member')?['statusCode'],204),contains(string(body('Add_member')),'already exist'))",
    f"@{{concat({P}?['group_name'], ' (HTTP ', string(outputs('Add_member')?['statusCode']), ')')}}"))

# ---------------------------------------------------------------- offboarding
steps = f"{P}?['offboarding_steps']"
off = {}
off["If_disable"] = {"type": "If", "runAfter": {},
    "expression": {"and": [{"contains": [f"@{steps}", "disable_account"]}]},
    "actions": {
        "Disable_user": http("PATCH", f"@{{concat('{GRAPH}/v1.0/users/', {UID})}}", MI_GRAPH, {},
                             headers={"Content-Type": "application/json"}, body={"accountEnabled": False}),
        **log("Log_disable", done("Disable_user"), "disable_account",
              "@equals(outputs('Disable_user')?['statusCode'],204)",
              "@{concat('HTTP ', string(outputs('Disable_user')?['statusCode']))}")},
    "else": {"actions": {}}}
off["If_revoke"] = {"type": "If", "runAfter": ok("If_disable"),
    "expression": {"and": [{"contains": [f"@{steps}", "revoke_sessions"]}]},
    "actions": {
        "Revoke_sessions": http("POST", f"@{{concat('{GRAPH}/v1.0/users/', {UID}, '/revokeSignInSessions')}}", MI_GRAPH, {},
                                headers={"Content-Type": "application/json"}, body={}),
        **log("Log_revoke", done("Revoke_sessions"), "revoke_sessions",
              "@equals(outputs('Revoke_sessions')?['statusCode'],200)",
              "@{concat('HTTP ', string(outputs('Revoke_sessions')?['statusCode']))}")},
    "else": {"actions": {}}}
per_group = {
    "Get_group_type": http("GET", "@{concat('" + GRAPH + "/v1.0/groups/', items('For_each_group')?['id'])}", MI_GRAPH, {},
                           queries={"$select": "id,displayName,groupTypes"}),
    "If_static_group": {"type": "If", "runAfter": ok("Get_group_type"),
        "expression": {"and": [{"not": {"contains": ["@body('Get_group_type')?['groupTypes']", "DynamicMembership"]}}]},
        "actions": {
            "Remove_member": http("DELETE", f"@{{concat('{GRAPH}/v1.0/groups/', items('For_each_group')?['id'], '/members/', {UID}, '/$ref')}}", MI_GRAPH, {}),
            **log("Log_remove", done("Remove_member"), "remove_group",
                  "@equals(outputs('Remove_member')?['statusCode'],204)",
                  "@{concat(items('For_each_group')?['displayName'], ' (HTTP ', string(outputs('Remove_member')?['statusCode']), ')')}")},
        "else": {"actions": log("Log_dynamic_skipped", {}, "remove_group", True,
                  "@{concat(items('For_each_group')?['displayName'], ' (dynamic group: membership rule-based, skipped)')}")}},
}
off["If_remove_groups"] = {"type": "If", "runAfter": ok("If_revoke"),
    "expression": {"and": [{"contains": [f"@{steps}", "remove_groups"]}]},
    "actions": {"For_each_group": {"type": "Foreach", "foreach": "@body('Filter_groups')", "runAfter": {},
                                   "runtimeConfiguration": {"concurrency": {"repetitions": 1}}, "actions": per_group}},
    "else": {"actions": {}}}

precheck = [
    "if(greater(length(body('Filter_roles')),0),createArray('target_privileged_role'),json('[]'))",
    f"if(not(contains(createArray({','.join(repr(x) for x in IMPLEMENTED)}),{A})),createArray('action_not_implemented'),json('[]'))",
    f"if(and(equals({A},'group_add'),not(contains(parameters('allowedGroupNames'),{P}?['group_name']))),createArray('group_not_allowlisted'),json('[]'))",
    f"if(not(equals({IT}?['proposalStatus'],'pending_review')),createArray('proposal_not_pending'),json('[]'))",
    f"if(not(equals({IT}?['reviewStatus'],'validated')),createArray('not_validated'),json('[]'))",
]

scope_actions = {
    "Get_subject_user": http("GET", f"@{{concat('{GRAPH}/v1.0/users/', encodeUriComponent(coalesce({IT}?['subjectEmail'],'unknown')))}}",
                             MI_GRAPH, {}, queries={"$select": "id,displayName,accountEnabled"}),
    "Get_subject_memberOf": http("GET", f"@{{concat('{GRAPH}/v1.0/users/', {UID}, '/memberOf')}}", MI_GRAPH,
                                 ok("Get_subject_user"), queries={"$select": "id,displayName"}),
    "Filter_roles": {"type": "Query", "runAfter": ok("Get_subject_memberOf"), "inputs": {
        "from": "@body('Get_subject_memberOf')?['value']", "where": "@equals(item()?['@odata.type'],'#microsoft.graph.directoryRole')"}},
    "Filter_groups": {"type": "Query", "runAfter": ok("Filter_roles"), "inputs": {
        "from": "@body('Get_subject_memberOf')?['value']", "where": "@equals(item()?['@odata.type'],'#microsoft.graph.group')"}},
    # Last-moment re-check of the guards (the directory may have changed since the proposal)
    "Compose_precheck": {"type": "Compose", "runAfter": ok("Filter_groups"), "inputs": "@union(" + ",".join(precheck) + ")"},
    "If_can_execute": {"type": "If", "runAfter": ok("Compose_precheck"),
        "expression": {"and": [{"equals": ["@length(outputs('Compose_precheck'))", 0]}, {"equals": ["@parameters('dryRun')", False]}]},
        "actions": {"Switch_action": {"type": "Switch", "runAfter": {}, "expression": f"@{A}",
                    "cases": {"Case_group_add": {"case": "group_add", "actions": group_add},
                              "Case_offboarding": {"case": "offboarding", "actions": off}},
                    "default": {"actions": {}}}},
        "else": {"actions": log("Log_not_executed", {}, "not_executed",
                  "@equals(length(outputs('Compose_precheck')),0)",
                  f"@{{if(greater(length(outputs('Compose_precheck')),0),concat('blocked: ', join(outputs('Compose_precheck'), ',')),concat('dry run - would execute: ', string({P})))}}")}},
}

status_expr = ("@if(greater(length(outputs('Compose_precheck')),0),'blocked',if(parameters('dryRun'),'dry_run',"
               "if(equals(length(body('Filter_failed_steps')),0),'success','partial')))")
note_expr = ("@{concat('[KnowledgeEngine] ', outputs('Compose_exec_status'), ' - action ', " + A + ", ' validee par ', "
             + IT + "?['reviewedByName'], ' (', " + IT + "?['reviewedAtUtc'], '). ', join(body('Select_log_lines'), ' | '))}")

ticket_actions = {
    "Reset_log": {"type": "SetVariable", "runAfter": {}, "inputs": {"name": "stepLog", "value": "@json('[]')"}},
    # Claim the row (If-Match on the ETag read by Get_rows): a row is executed at most once
    "Claim_row": http("POST", row_uri, MI_STORAGE, ok("Reset_log"),
                      headers=dict(merge_headers, **{"If-Match": f"@{IT}?['odata.etag']"}),
                      body={"executionStatus": "running", "executionStartedUtc": "@{utcNow()}"}),
    "Compose_params": {"type": "Compose", "runAfter": ok("Claim_row"), "inputs": f"@json({IT}?['approvedParamsJson'])"},
    "Scope_execute": {"type": "Scope", "runAfter": ok("Compose_params"), "actions": scope_actions},
    "Filter_failed_steps": {"type": "Query", "runAfter": ok("Scope_execute"),
                            "inputs": {"from": "@variables('stepLog')", "where": "@not(equals(item()?['ok'], true))"}},
    "Compose_exec_status": {"type": "Compose", "runAfter": ok("Filter_failed_steps"), "inputs": status_expr},
    "Select_log_lines": {"type": "Select", "runAfter": ok("Compose_exec_status"), "inputs": {
        "from": "@variables('stepLog')",
        "select": "@concat(item()?['step'], if(equals(item()?['ok'], true), ' OK ', ' ECHEC '), item()?['detail'])"}},
    # ServiceNow: close the RITM only on full success; otherwise leave it open with a work note.
    # Nothing is written to ServiceNow in dry run. Work notes never contain secrets.
    "If_update_servicenow": {"type": "If", "runAfter": ok("Select_log_lines"),
        "expression": {"and": [{"equals": ["@parameters('dryRun')", False]}]},
        "actions": {"If_close": {"type": "If", "runAfter": {},
            "expression": {"and": [{"equals": ["@outputs('Compose_exec_status')", "success"]}, {"equals": [f"@{IT}?['ticketType']", "ritm"]}]},
            "actions": {"Close_ritm": http("PATCH",
                "@{concat('https://', parameters('snInstance'), '.service-now.com/api/now/table/sc_req_item/', " + IT + "?['snSysId'])}",
                {"type": "Basic", "username": "@parameters('snUser')", "password": "@body('Get_sn_secret')?['value']"}, {},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body={"state": "3", "work_notes": note_expr}, secure_inputs=True)},
            "else": {"actions": {"Add_work_note": http("PATCH",
                "@{concat('https://', parameters('snInstance'), '.service-now.com/api/now/table/', if(equals(" + IT + "?['ticketType'],'ritm'),'sc_req_item','incident'), '/', " + IT + "?['snSysId'])}",
                {"type": "Basic", "username": "@parameters('snUser')", "password": "@body('Get_sn_secret')?['value']"}, {},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                body={"work_notes": note_expr}, secure_inputs=True)}}}},
        "else": {"actions": {}}},
    "Merge_result": http("POST", row_uri, MI_STORAGE, {"If_update_servicenow": ["Succeeded", "Failed"]}, headers=merge_headers, body={
        "executionStatus": "@{outputs('Compose_exec_status')}",
        "executionLog": "@{string(variables('stepLog'))}",
        "executionDryRun": "@parameters('dryRun')",
        "snUpdateOk": "@equals(actions('If_update_servicenow')?['status'], 'Succeeded')",
        "executedAtUtc": "@{utcNow()}"}),
    "Merge_error": http("POST", row_uri, MI_STORAGE, {"Scope_execute": ["Failed", "TimedOut"]}, headers=merge_headers, body={
        "executionStatus": "error",
        "executionLog": "@{string(variables('stepLog'))}",
        "executionError": "Unexpected failure in Scope_execute - see the Logic App run history",
        "executedAtUtc": "@{utcNow()}"}),
}

wf = {
    "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {k: {"type": t} for k, t in [
        ("clientCode", "string"), ("storageAccountName", "string"), ("tableName", "string"),
        ("snInstance", "string"), ("snUser", "string"), ("keyVaultName", "string"), ("snSecretName", "string"),
        ("allowedGroupNames", "array"), ("dryRun", "bool")]},
    "triggers": {"Recurrence": {"type": "Recurrence", "recurrence": {"frequency": "Minute", "interval": 2},
                                "runtimeConfiguration": {"concurrency": {"runs": 1}}}},
    "actions": {
        "Init_stepLog": {"type": "InitializeVariable", "runAfter": {}, "inputs": {"variables": [{"name": "stepLog", "type": "array", "value": []}]}},
        "Get_rows": http("GET", "@{concat('https://', parameters('storageAccountName'), '.table.core.windows.net/', parameters('tableName'), '()')}",
                         MI_STORAGE, ok("Init_stepLog"),
                         queries={"$filter": "@{concat('PartitionKey eq ''', parameters('clientCode'), ''' and reviewStatus eq ''validated''')}"},
                         headers=TABLE_HEADERS),
        "Filter_to_execute": {"type": "Query", "runAfter": ok("Get_rows"),
            # A row simulated while dryRun=true is executed for real once dryRun=false (no manual reset needed)
            "inputs": {"from": "@body('Get_rows')?['value']",
                       "where": "@or(empty(item()?['executionStatus']),and(equals(item()?['executionStatus'],'dry_run'),equals(parameters('dryRun'),false)))"}},
        "Get_sn_secret": http("GET", "@{concat('https://', parameters('keyVaultName'), '.vault.azure.net/secrets/', parameters('snSecretName'), '?api-version=7.4')}",
                              {"type": "ManagedServiceIdentity", "audience": "https://vault.azure.net"}, ok("Filter_to_execute")),
        "For_each_ticket": {"type": "Foreach", "foreach": "@body('Filter_to_execute')", "runAfter": ok("Get_sn_secret"),
                            "runtimeConfiguration": {"concurrency": {"repetitions": 1}}, "actions": ticket_actions},
    },
    "outputs": {},
}
wf["actions"]["Get_sn_secret"]["runtimeConfiguration"] = {"secureData": {"properties": ["outputs"]}}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow-definition.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(wf, f, indent=2, ensure_ascii=True)
print("written", out)
