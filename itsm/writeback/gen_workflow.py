# Generates workflow-definition.json for the DIAGNOSTIC WRITE-BACK Logic App (V10 slice 6).
# Edit this file, then: python itsm/writeback/gen_workflow.py  (and redeploy itsm/writeback/main.bicep)
#
# What it does, every 2 minutes, for the rows of ONE client (parameter clientId) of the diagwriteback
# table that an ITSM agent VALIDATED in the web app's Diagnostic and that were not executed yet
# (status = 'validated', executionStatus = '' -- the filter is in the query, 50 rows per run at most):
#   1. claims the row (MERGE executionStatus=running, If-Match on its ETag): executed at most once;
#   2. finds the incident by its number in ServiceNow (Table API, svc_ke_itsm, password from Key Vault);
#      an incident that is not active any more is left alone (status 'inactive');
#   3. kind 'work_note': adds the row's note as an internal work note (never a customer-visible comment);
#      kind 'handover': same note, and the incident is assigned to the ITSM module's group
#      (KE-Automation), where the ITSM action engine picks it up (poll -> propose -> agent -> execute);
#      the group must exist, else the row ends in 'error' (never a silent success);
#   4. writes executionStatus (success / not_found / inactive / dry_run / error) back into the row.
# The note is written by the web app from the session's fiche and steps only (orchestration/guide/
# writeback.py), never from what the user typed. dryRun=true (default): nothing is written to ServiceNow,
# rows end in 'dry_run'; once deployed with dryRun=false, the dry_run rows of the last 2 days are
# written for real (older ones stay simulated: an agent validates them again if still relevant).
# ServiceNow answers are limited to sys_id/number/active and hidden from the run history (secure data).
import json
import os

MI_STORAGE = {"type": "ManagedServiceIdentity", "audience": "https://storage.azure.com"}
MI_VAULT = {"type": "ManagedServiceIdentity", "audience": "https://vault.azure.net"}
SN_AUTH = {"type": "Basic", "username": "@parameters('snUser')", "password": "@body('Get_sn_secret')?['value']"}
SECURE_BOTH = {"secureData": {"properties": ["inputs", "outputs"]}}
SECURE_OUT = {"secureData": {"properties": ["outputs"]}}
IT = "items('For_each_row')"
SN = "@{concat('https://', parameters('snInstance'), '.service-now.com/api/now/table/"
TABLE_HEADERS = {"x-ms-version": "2020-12-06", "Accept": "application/json;odata=minimalmetadata"}
MERGE_HEADERS = {"x-ms-version": "2020-12-06", "Accept": "application/json;odata=nometadata",
                 "X-HTTP-Method": "MERGE", "Content-Type": "application/json"}
SN_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
ROW_URI = ("@{concat('https://', parameters('storageAccountName'), '.table.core.windows.net/', parameters('tableName'), "
           "'(PartitionKey=''', " + IT + "?['PartitionKey'], ''',RowKey=''', " + IT + "?['RowKey'], ''')')}")
SYS_ID = "outputs('Compose_sys_id')"
ACTIVE = "outputs('Compose_active')"
# Rows to execute, in the query itself (processed rows never come back, whatever the table's size):
#   dryRun : PartitionKey eq '<client>' and status eq 'validated' and executionStatus eq ''
#   live   : ... and (executionStatus eq '' or (executionStatus eq 'dry_run' and validatedAtUtc ge '<2 days ago>'))
ROWS_FILTER = ("@{concat('PartitionKey eq ''', parameters('clientId'), ''' and status eq ''validated'' and ', "
               "if(parameters('dryRun'), 'executionStatus eq ''''', "
               "concat('(executionStatus eq '''' or (executionStatus eq ''dry_run'' and validatedAtUtc ge ''', "
               "formatDateTime(addDays(utcNow(), -2), 'yyyy-MM-ddTHH:mm:ssZ'), '''))')))}")


def http(method, uri, auth, run_after, queries=None, headers=None, body=None, runtime=None):
    action = {"type": "Http", "inputs": {"method": method, "uri": uri, "authentication": auth}, "runAfter": run_after}
    if queries:
        action["inputs"]["queries"] = queries
    if headers:
        action["inputs"]["headers"] = headers
    if body is not None:
        action["inputs"]["body"] = body
    if runtime:
        action["runtimeConfiguration"] = runtime
    return action


def ok(*names):
    return {n: ["Succeeded"] for n in names}


def any_end(*names):
    return {n: ["Succeeded", "Failed", "Skipped", "TimedOut"] for n in names}


patch_uri = SN + "incident/', " + SYS_ID + ")}"


def set_status(name, run_after, value):
    return {name: {"type": "SetVariable", "runAfter": run_after, "inputs": {"name": "rowStatus", "value": value}}}


def patch(name, body):
    return http("PATCH", patch_uri, SN_AUTH, {}, queries={"sysparm_fields": "sys_id"}, headers=SN_HEADERS, body=body,
                runtime=SECURE_BOTH)


def status_of(action):
    return f"@{{if(equals(outputs('{action}')?['statusCode'], 200), 'success', 'error')}}"


# Every status is set in the scope of the action it judges (no reference to an action of another
# branch, nothing read from a skipped action); a row starts as 'error' until proven otherwise.
handover_patch = patch("Patch_handover", {"work_notes": f"@{IT}?['noteText']",
                                          "assignment_group": "@first(body('Get_handover_group')?['result'])?['sys_id']"})
sn_actions = {
    "Get_incident": http("GET", SN + "incident')}", SN_AUTH, {},
                         queries={"sysparm_query": f"@{{concat('number=', {IT}?['ticketNumber'])}}",
                                  "sysparm_fields": "sys_id,number,active", "sysparm_limit": "1"},
                         headers=SN_HEADERS, runtime=SECURE_BOTH),
    "Compose_sys_id": {"type": "Compose", "runAfter": ok("Get_incident"),
                       "inputs": "@coalesce(first(body('Get_incident')?['result'])?['sys_id'], '')"},
    "Compose_active": {"type": "Compose", "runAfter": ok("Compose_sys_id"),
                       "inputs": "@string(coalesce(first(body('Get_incident')?['result'])?['active'], ''))"},
    "If_found_active_and_live": {"type": "If", "runAfter": ok("Compose_active"),
        "expression": {"and": [{"not": {"equals": [f"@{SYS_ID}", ""]}}, {"equals": [f"@{ACTIVE}", "true"]},
                               {"equals": ["@parameters('dryRun')", False]}]},
        "actions": {"If_handover": {"type": "If", "runAfter": {},
            "expression": {"and": [{"equals": [f"@{IT}?['kind']", "handover"]}]},
            "actions": {
                "Get_handover_group": http("GET", SN + "sys_user_group')}", SN_AUTH, {},
                    queries={"sysparm_query": "@{concat('name=', parameters('handoverGroup'))}",
                             "sysparm_fields": "sys_id", "sysparm_limit": "1"}, headers=SN_HEADERS, runtime=SECURE_BOTH),
                "If_group_found": {"type": "If", "runAfter": ok("Get_handover_group"),
                    "expression": {"and": [{"greater": [
                        "@length(coalesce(body('Get_handover_group')?['result'], json('[]')))", 0]}]},
                    "actions": {
                        "Patch_handover": handover_patch,
                        **set_status("Status_handover", {"Patch_handover": ["Succeeded", "Failed"]},
                                     status_of("Patch_handover"))},
                    "else": {"actions": set_status("Status_no_group", {}, "error")}}},
            "else": {"actions": {
                "Patch_note": patch("Patch_note", {"work_notes": f"@{IT}?['noteText']"}),
                **set_status("Status_note", {"Patch_note": ["Succeeded", "Failed"]}, status_of("Patch_note"))}}}},
        "else": {"actions": set_status("Status_not_written", {},
                                       f"@{{if(equals({SYS_ID}, ''), 'not_found', "
                                       f"if(equals({ACTIVE}, 'true'), 'dry_run', 'inactive'))}}")}},
}

row_actions = {
    **set_status("Reset_status", {}, "error"),
    "Claim_row": http("POST", ROW_URI, MI_STORAGE, ok("Reset_status"),
                      headers=dict(MERGE_HEADERS, **{"If-Match": f"@{IT}?['odata.etag']"}),
                      body={"executionStatus": "running", "executionStartedUtc": "@{utcNow()}"}),
    "Scope_servicenow": {"type": "Scope", "runAfter": ok("Claim_row"), "actions": sn_actions},
    "Merge_result": http("POST", ROW_URI, MI_STORAGE, any_end("Scope_servicenow"), headers=MERGE_HEADERS, body={
        "executionStatus": "@{variables('rowStatus')}",
        "executionDryRun": "@parameters('dryRun')",
        "executedAtUtc": "@{utcNow()}"}),
}

wf = {
    "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {name: {"type": kind} for name, kind in [
        ("storageAccountName", "string"), ("tableName", "string"), ("clientId", "string"), ("snInstance", "string"),
        ("snUser", "string"), ("keyVaultName", "string"), ("snSecretName", "string"), ("handoverGroup", "string"),
        ("dryRun", "bool")]},
    "triggers": {"Recurrence": {"type": "Recurrence", "recurrence": {"frequency": "Minute", "interval": 2},
                                "runtimeConfiguration": {"concurrency": {"runs": 1}}}},
    "actions": {
        "Init_status": {"type": "InitializeVariable", "runAfter": {},
                        "inputs": {"variables": [{"name": "rowStatus", "type": "string", "value": "error"}]}},
        "Get_rows": http("GET", "@{concat('https://', parameters('storageAccountName'), '.table.core.windows.net/', parameters('tableName'), '()')}",
                         MI_STORAGE, ok("Init_status"), queries={"$filter": ROWS_FILTER, "$top": "50"},
                         headers=TABLE_HEADERS),
        "If_any": {"type": "If", "runAfter": ok("Get_rows"),
            "expression": {"and": [{"greater": ["@length(coalesce(body('Get_rows')?['value'], json('[]')))", 0]}]},
            "actions": {
                "Get_sn_secret": http("GET", "@{concat('https://', parameters('keyVaultName'), '.vault.azure.net/secrets/', parameters('snSecretName'), '?api-version=7.4')}",
                                      MI_VAULT, {}, runtime=SECURE_OUT),
                "For_each_row": {"type": "Foreach", "foreach": "@body('Get_rows')?['value']", "runAfter": ok("Get_sn_secret"),
                                 "runtimeConfiguration": {"concurrency": {"repetitions": 1}}, "actions": row_actions}},
            "else": {"actions": {}}},
    },
    "outputs": {},
}

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workflow-definition.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(wf, f, indent=2, ensure_ascii=True)
print("written", out)
