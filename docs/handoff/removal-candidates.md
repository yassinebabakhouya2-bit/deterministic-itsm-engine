# Removal candidates and verifier verdicts

Mapping done 2026-10-15 by 6 readers (before the decisions to drop audio/video and to merge duplicates). Verdicts: `dead_safe_to_remove` = no live
reference found; `still_referenced` = something live uses it (list the references and edit them first); `feature_keep` = working feature, keep.
Verifiers ran only for webapp, engine and infra areas.

## Verified: webapp

- **dead_safe_to_remove** `app/app.py (classic RAG assistant: lines 1-116 header, 169-196, 265-1022, 1063-1195, 1206-1223)`
  - live references: app/diag_tab.py:517 (the only link to /classic, on the Diagnostic home page); app/app.py:1076-1078 (any POST to /classic or /c/<id> redirects to diag.home before any a…; app/app.py:1194 (always rendered with read_only=True); app/app.py:175-176, 181-196 (creates and opens the convindex/convturns tables, used only …; app/app.py:369-370 _user_partition plus app/auth.py:166-173 resolve_user_id (rows are key…; app/app.py:128-129, 304-309 (the only imports and use of markdown and nh3 in the repo)
  - if removed: The page is dead. Its answering code cannot be reached, because every POST is redirected at app.py:1076-1078. What is left only reads and deletes conversations saved in convindex/convturns in the disabled stknowledgeengine3v9 account. Copying those rows would not help: their PartitionKey is '<clientId>:<oid>', and the brand-new tenant gives every user a new oid, so no row would ever match a signed-in user. The free-…
- **dead_safe_to_remove** `app/diag_tab.py (POST /api/servicenow/webhook, POST /diag/internal/sweep) + scripts/enable-diagnostic-webhook.ps1`
  - live references: app/diag_tab.py:411-450 (_signed_body, webhook, sweep); app/diag_tab.py:127-136 verify_signature (used only by _signed_body); app/diag_tab.py:10-11, 29-31 (header route list and security comment); app/diag_tab.py:42-43, 45, 47, 49 and jsonify at :55 (hashlib, hmac, json, os, time, json…; app/diag_tab.py:439-440 (the only caller passing session_id/event_id/origin='servicenow' …; orchestration/guide/service.py:193-194 sweep() returns 0
  - if removed: Nothing live depends on it. No file in the repo calls either route: none of the itsm/ Logic Apps (poll, propose, execute, writeback), no Bicep, and no script other than the enabling one. In any deployment built from Bicep the routes cannot be reached. DIAG_WEBHOOK_SECRET is never set, so _signed_body aborts with 404. Easy Auth requireAuthentication (webapp.bicep:122-123) blocks any caller without an Entra session, a…
- **still_referenced** `config/engine.clienta.yaml, config/engine.clientb.yaml, config/engine.clientc.yaml`
  - live references: scripts/bootstrap-new-tenant.ps1:9, :70 (default -Clients = clienta, clientb, clientc, cl…; scripts/bootstrap-new-tenant.ps1:221-224 Get-ClientConfigPath throws 'Missing config/engi…; scripts/bootstrap-new-tenant.ps1:798-820 (data phase uploads kb/<c> and kb/clientc-multif…; scripts/bootstrap-new-tenant.ps1:826-828 (search phase runs search/deploy.ps1 for each cl…; scripts/bootstrap-new-tenant.ps1:263-270 Get-AllowedTenants globs config/engine.*.yaml; :…; app/auth.py:74-76, 88 build_access_maps globs config/engine.*.yaml (these clients appear …
  - if removed: These are the synthetic demo clients of two features that decision (b) keeps: multi-client support and RAG/OPEN answers over Azure AI Search. They are also wired into the new-tenant deployment script. Delete them as-is and scripts/bootstrap-new-tenant.ps1, run with its default -Clients, throws in the entra phase (Get-ClientConfigPath :223). They are also the only clients whose KB is in git (kb/client{a,b,c}, kb/clie…
- **dead_safe_to_remove** `demo/ (build_demo.py, index.html)`
  - live references: README.md:92 ('demo/ Static demo build'); CLAUDE.md:81 (language rule names 'the demo/ UI copy'); demo/build_demo.py:15-18 reads kb/client{a,b,c} (a one-way read; kb/ is kept for the boot…
  - if removed: Nothing. It is an offline HTML mock generated on the operator's machine that simulates isolation in browser JavaScript. It is not part of the deployed system. deploy-webapp.ps1:52 and :61 do not package it (only app, orchestration, config, requirements.txt, README.md). No Bicep, PowerShell (bootstrap included), Python import, test or Function references it. Its last change was commit e5f664b (Jalon 5). Remove it tog…
- **feature_keep** `tests/ (test_guide_fsm.py, test_kefind_ports.py, test_kecore_client.py, test_labels_app.py, test_dictionary_app.py, test_review_app.py, tes…`
  - live references: tests/test_guide_fsm.py:9-10 -> orchestration/guide/contracts.py, fsm.py; tests/test_kefind_ports.py:9-11 -> orchestration/guide/kefind_ports.py; tests/test_kecore_client.py:9 -> app/kecore_client.py; tests/test_labels_app.py:12 -> app/labels.py; tests/test_dictionary_app.py:10 -> app/dictionary_tab.py; tests/test_review_app.py:13-17 -> app/dictionary_tab.py, itsm.py, kecore_client.py, label…
  - if removed: They test modules that stay: the Diagnostic, labels, dictionary and review tabs, the kecore client, the guide FSM, kefind ports and write-back. ITSM and write-back are kept features under decision (b), so test_review_app.py:14, 828-831, test_guide_app.py:280-460 and test_writeback.py all stay. Removing tests/ would drop the only coverage of the Web App tabs and the guide engine. No deploy depends on them, because de…
## Verified: engine

- **feature_keep** `kecore_func/tests/`
  - live references: kecore_func/README.md:292; kecore_func/tests/test_kecore_pipeline.py:23; kecore_func/tests/test_routing_end_to_end.py:50; kecore_func/tests/test_routing_end_to_end.py:53; kecore_func/tests/test_routing_end_to_end.py:54; kecore_func/tests/test_routing_service.py:28
  - if removed: Keep this folder. It tests the live Function and is the only proof that the Function works without Azure. I re-ran it: 295 OK on python3.13 with the azure libraries, and 213 OK with 2 skipped on /usr/bin/python3.11. Nothing in Azure depends on it, because deploy-kecore-function.ps1:53 leaves tests/ and examples/ out of the zip. It depends on more than the other reader listed. Besides kecore/tests/helpers.py and kefi…
- **still_referenced** `kecore/pending.py`
  - live references: kecore_func/dictionary_service.py:34; kecore_func/dictionary_service.py:37; kecore_func/function_app.py:102; kecore/tests/test_pending.py:3
  - if removed: Deleting it as it stands breaks the Function. dictionary_service.py:34 runs `from kecore.pending import DEFAULT_MIN_OBSERVATIONS` at module load, and function_app.py:102 imports dictionary_service, so the host would load no functions at all. Simulated removal gives 85 errors in kecore_func/tests: test_dictionary_service fails to import, and test_function_app and test_routing_end_to_end error. The rest of the module …
- **dead_safe_to_remove** `kecore/cli.py, kecore/__main__.py`
  - live references: kecore/__main__.py:3; kecore/tests/test_cli.py:7; kecore/README.md:39; scripts/deploy-kecore-function.ps1:61; docs/v10-deterministic-engine.md:113; docs/operations-runbook.md:1859
  - if removed: Nothing live breaks. - kecore.cli is not in the Function's import closure (computed transitively from kecore_func/*.py). It is shipped by deploy-kecore-function.ps1:61 but never imported. - The Web App (app/ and orchestration/) imports no kecore module. - No script, Bicep, YAML or runbook step runs `python -m kecore`. - Simulated removal: 295/295 Function tests OK; only kecore/tests/test_cli.py (3 tests) fails. Remo…
- **still_referenced** `kecore/tests/`
  - live references: kecore_func/tests/test_kecore_pipeline.py:23; kecore_func/tests/test_routing_end_to_end.py:50; kecore/tests/helpers.py:8; kecore/README.md:69
  - if removed: The folder cannot go. kecore/tests/helpers.py is imported by two Function test modules, and its DEMO_KB points at kecore/examples/kb-demo. Most of the folder covers live code: test_decompose, test_entities, test_exclusion, test_llm, test_profile, test_segment, test_text and test_tickets. kecore.tickets is used by tickets_service.py:31-32 and scoreboard_service.py:57. The other reader is wrong about two files: - test…
- **still_referenced** `kefind/{engine,compose,decide,graph_filter,calibration,understand}.py`
  - live references: kefind/search.py:32; kefind/funnel.py:52; kefind/graph.py:30; kefind/calibrate.py:48; kecore_func/kefind_service.py:31; kecore_func/function_app.py:105
  - if removed: Removed as one group, these break the Function, because of understand.py. kefind/search.py:32 imports TicketUnderstanding at module load, and search.py is live: funnel.py:52 (SEARCHABLE_STATUSES, tokenize), graph.py:30 and calibrate.py:48 import it. The path is function_app.py:105 -> kefind_service.py:31 -> kefind.funnel -> kefind.search -> kefind.understand. Simulated removal of understand.py: all 9 kecore_func tes…
- **dead_safe_to_remove** `kefind/{engine,compose,decide,graph_filter,calibration}.py`
  - live references: kefind/cli.py:22; kefind/cli.py:23; kefind/cli.py:24; kefind/engine.py:17; kefind/engine.py:18; kefind/engine.py:20
  - if removed: This entry splits out the five modules of the group above that are really dead (understand.py is excluded). Nothing live breaks. - None of them is in the Function's import closure. - kefind/cli.py:22-24 is the only importer of engine, calibration and decide. engine.py:17-20 is the only importer of compose and graph_filter. - No JSON or YAML config in the repo names 'kefind.engine:factory'; only kefind/README.md:170 …
- **still_referenced** `kefind/io.py`
  - live references: kefind/funnel_engine.py:17; kecore_func/scoreboard_service.py:58; kecore_func/function_app.py:107; kefind/tests/test_funnel.py:8; kecore_func/tests/test_routing_service.py:29; kefind/tests/test_io.py:8
  - if removed: The other reader's evidence is wrong: this module IS in the Function's import closure. kefind/funnel_engine.py:17 imports load_decomposed_jsonl at module load, scoreboard_service.py:58 imports funnel_engine, and function_app.py:107 imports scoreboard_service. Simulated removal: 82 errors in kecore_func/tests (routing, scoreboard, semantic, tickets, function_app), and 13 kefind test modules fail, including the kept t…
- **dead_safe_to_remove** `kefind/cli.py, kefind/__main__.py`
  - live references: kefind/__main__.py:3; kefind/tests/test_cli.py:5; kefind/README.md:182
  - if removed: Nothing live breaks. It is only reached by `python -m kefind`, which no script, Bicep file or runbook step runs, and the Web App imports no kefind module. The default cache dirs clients-local/kefind/llm-cache/{understand,write} are confirmed at cli.py:108-109. Simulated removal: only kefind/tests/test_cli.py (7 tests) fails; 295/295 Function tests OK. Remove it in the same change as the five dead legacy modules, bec…
- **dead_safe_to_remove** `scoreboard/cli.py, scoreboard/__main__.py, scoreboard/importers.py, scoreboard/labeling.py, scoreboard/ids.py`
  - live references: scoreboard/__main__.py:3; scoreboard/cli.py:25; scoreboard/cli.py:26; scoreboard/cli.py:27; scoreboard/labeling.py:27; scoreboard/importers.py:26
  - if removed: Nothing live breaks. The Function imports only scoreboard.dataset, metrics, pricing, report, runner and engines/__init__ (scoreboard_service.py:62-67, runner.py:20). The Web App imports no scoreboard module. Simulated removal of all five: 295/295 Function tests OK and the kecore and kefind suites OK; only the scoreboard tests test_cli, test_importers, test_labeling and test_search_baseline fail. scoreboard/ids.py is…
- **dead_safe_to_remove** `scoreboard/engines/search_baseline.py`
  - live references: scoreboard/engines/__init__.py:36; scoreboard/cli.py:25; scoreboard/tests/test_search_baseline.py:8; scoreboard/examples/search-baseline.example.json; scoreboard/README.md:72
  - if removed: It is not a product feature: it is a measurement baseline driven only by the local CLI (load_engine by name, and inspect-index). - The live RAG fallback over Azure AI Search is in orchestration/answer.py. Nothing in app/ or orchestration/ imports kecore, kefind or scoreboard. - The Function's scoreboard only builds FunnelEngine (scoreboard_service.py:341,361). - The labeling tab's candidates come from fn-kecore (`ke…
- **still_referenced** `scoreboard/engines/fixture.py`
  - live references: scoreboard/tests/test_route_records.py:13; scoreboard/tests/test_route_records.py:190; scoreboard/engines/__init__.py:37; scoreboard/tests/test_engines.py:8; scoreboard/tests/test_engines.py:39; scoreboard/tests/test_engines.py:73
  - if removed: Nothing uses it at runtime: the Function never imports it, and although it ships in the zip it is inert. But kept tests of live runner, report and engines code depend on it. Simulated removal: test_route_records (15 tests) and test_engines (10) fail to import. It can move under scoreboard/tests/ only with these edits: 1. Drop or repoint the 'fixture' string registry entry at engines/__init__.py:37. 2. Update the imp…
- **feature_keep** `scoreboard/tests/`
  - live references: scoreboard/README.md:98; scoreboard/tests/helpers.py:24; scoreboard/tests/test_routing_gate.py:74; scoreboard/tests/test_route_records.py:187
  - if removed: Keep this folder. It is the test suite of the scoreboard code the Function runs: metrics, routing gate, report, runner and dataset (scoreboard_service.py:62-67). Keep: - test_metrics (10 tests), test_routing_gate (58), test_route_records (15) and test_dataset (11). - test_engines (10). It covers the live Decision, decision_from_dict, run_engine and make_record, plus the CLI-only load_engine, expand_paths, manifest_p…
- **still_referenced** `kefind/tests/`
  - live references: kecore_func/tests/test_tickets_service.py:20; kecore_func/tests/test_scoreboard_service.py:230; kecore_func/tests/test_scoreboard_service.py:333; kecore_func/tests/test_scoreboard_service.py:334; kecore_func/tests/test_semantic_service.py:24; kecore_func/tests/test_semantic_service.py:25
  - if removed: The folder must stay. Function tests import kefind/tests/helpers.py, schema_llm.py and semantic_helpers.py, and also test_funnel.py (`make`, at test_routing_service.py:29). Kept kefind tests import each other too: test_assign, test_calibrate, test_enrich, test_review, test_route, test_routing, test_semantic and test_taxonomy import test_funnel, test_enrich, test_semantic and test_routing. Delete now, together with t…
## Verified: infra

- **dead_safe_to_remove** `infra/deploy-tenants.local.json`
  - live references: docs/operations-runbook.md:545-550 (§6.3, cited only as the 'pattern' for a parameters fi…
  - if removed: Nothing breaks. No deployment path reads the file. scripts/bootstrap-new-tenant.ps1 works out the tenant list itself (Get-AllowedTenants at :263, called at :700), writes its own temporary ARM parameters file with easyAuthAllowedTenantIds (:704-723) and deploys infra/main.bicep with "@$paramsFile" (:736). scripts/attach-external-tenant.ps1 sets WEBSITE_AUTH_AAD_ALLOWED_TENANTS directly through its own temp file (:229…
- **still_referenced** `infra/modules/appsettings-merge.bicep`
  - live references: infra/modules/kecore-link.bicep:72 (module webAppSettings 'appsettings-merge.bicep'); infra/modules/kecore-link.bicep:14 (comment); docs/operations-runbook.md:2486 (19.6 deploy command for kecore-link), :2536 (19.8 incide…; app/kecore_client.py:127 (reads KECORE_FUNCTION_URL / KECORE_FUNCTION_KEY, which only thi…; app/app.py:214-216 (kecore_client.from_env()); kecore_func/README.md:177 - docs
  - if removed: Deleting the file breaks compilation of infra/modules/kecore-link.bicep, which is the only thing that sets KECORE_FUNCTION_URL and KECORE_FUNCTION_KEY (a Key Vault reference to secret kecore-function-key). The Web App reads these through app/kecore_client.py:127. Without them, the Diagnostic silently falls back to the search index only, and the /dictionary and review tabs show 'Le moteur n'est pas relié...' (app/dic…
- **feature_keep** `infra/modules/videoindexer.bicep`
  - live references: scripts/bootstrap-new-tenant.ps1:1064 (video phase deploys it; :1055-1062 registers Micro…; ingestion/video-index/main.bicep:32, :73, :178 (consumer Logic App expects the account an…; ingestion/workflow-definition.json:265 (SharePoint ingestion routes video extensions to v…; ingestion/main.bicep:34-35 (videoContainerName); scripts/sync-client.ps1:74, :592 (-WithMedia runs logic-video-index-<client>), :474; orchestration/answer.py:195-196, :849 (media filters, sourceType video in the RAG answer)
  - if removed: Video ingestion is a product feature that decision (b) keeps explicitly. Removing this module breaks the bootstrap 'video' phase (Invoke-BicepDeploy at :1064). Without a Video Indexer account, logic-video-index-<client> cannot index anything, and the video files the SharePoint ingestion routes to video-raw-<client> (ingestion/workflow-definition.json:265) are stranded. Its output is consumed by the RAG fallback and …
- **feature_keep** `ingestion/audio-transcribe/`
  - live references: scripts/bootstrap-new-tenant.ps1:1021-1045 (audio phase: speech-key from Foundry key1 at …; ingestion/workflow-definition.json:265 and ingestion/main.bicep:31-32 (audio extensions r…; scripts/sync-client.ps1:73, :473, :589 (logic-transcribe-<client>, -WithMedia, -Trace aud…; ingestion/audio-transcribe/workflow-definition.json:1213-1230 (writes <name>.txt into kb-…; search/skillset.template.json:74, :134 and search/indexer.template.json (ix-<client>-text…; enrichment/function_app.py:410, :497 (call enrichment reads the Probleme/Resolution heade…
  - if removed: Audio ingestion (Speech batch transcription, PII redaction, Problem/Resolution summary) is a product feature that decision (b) keeps explicitly. Removing it breaks the bootstrap 'audio' phase (:1039). Audio files that the SharePoint ingestion still routes to audio-raw-<client> would never be transcribed, and the audio branch of enrichment and the RAG media annexes in answer.py/app.py would be left with no producer. …
- **feature_keep** `ingestion/video-index/`
  - live references: scripts/bootstrap-new-tenant.ps1:1071 (video phase deploys ingestion/video-index/main.bic…; scripts/sync-client.ps1:74, :330-332, :474, :592-595 (logic-video-index-<client>, -WithMe…; ingestion/workflow-definition.json:265 and ingestion/main.bicep:34-35 (video files routed…; ingestion/workflow-definition.json:299 (videoindexed flag preserved by the HEAD check); orchestration/answer.py:195-196, :849; app/app.py:279, :834 ("une vidéo"); enrichment/function_app.py:497
  - if removed: This is the video ingestion feature that decision (b) keeps. Removing it breaks the bootstrap 'video' phase (:1071) and sync-client.ps1 -WithMedia / -Trace video (:592). Files in video-raw-<client>, which the SharePoint ingestion still writes (ingestion/workflow-definition.json:265), would never reach kb-<client>, ix-<client>-text or the RAG media annexes. 'The deterministic engine does not consume it' is accurate b…
- **dead_safe_to_remove** `scripts/backfill-pii-pipeline.ps1, scripts/check-transcribed-total.ps1, scripts/clear-transcribed-flag.ps1, scripts/repair-transcribed-flag…`
  - live references: docs/operations-runbook.md:831-855 (§9.2 'Reusable scripts', docs only); docs/operations-runbook.md:736-745 (§9 intro describing the repair/backfill operations), …; scripts/clear-transcribed-flag.ps1:6 -> repair-transcribed-flag.ps1; scripts/check-transc…
  - if removed: Nothing live invokes them. There is no reference from any other .ps1 (bootstrap-new-tenant.ps1 and sync-client.ps1 included), Bicep, Logic App definition, Python, test or README. All four hard-code $storageAccount = "stknowledgeengine3v9" (backfill-pii-pipeline.ps1:37, check-transcribed-total.ps1:19, clear-transcribed-flag.ps1:31, repair-transcribed-flag.ps1:37), which is the disabled subscription's account, so they…

## Reader claims (unverified for periphery, data, docs)

### periphery
- `app/diag_tab.py /diag/internal/sweep + orchestration/guide/service.py GuideService.sweep` (dead): Unreachable behaviour kept only for compatibility with a design that was removed. Drop the route, GuideService.sweep and the excludedPath. This sits outside this area's folders but is coupled to enable-diagnostic-webhook.ps1.
- `demo/ (build_demo.py, index.html)` (dead): A leftover pitch artifact from earlier milestones, superseded by the real app. Also drop the CLAUDE.md demo/ language exception.
- `scripts/check-transcribed-total.ps1` (dead): A one-off incident tool for data that will not exist in the new tenant.
- `scripts/backfill-pii-pipeline.ps1` (dead): A one-off migration/backfill for past data.
- `scripts/repair-transcribed-flag.ps1` (dead): A repair script for a fixed past incident.

### data
- `scripts/backfill-pii-pipeline.ps1, scripts/check-transcribed-total.ps1` (operator_tooling): Migration scripts for a past incident whose data no longer exists.
- `scoreboard/labeling.py, scoreboard/importers.py` (legacy_but_wired): Replaced by the /labels tab. It writes local files on the operator's machine (CLAUDE.md). It is not an import path for ticketlabels.
- `azure:stknowledgeengine3v9/kecore-client-s/find-cache/` (data): Do not migrate. The data is derived from personal data and the keys cannot replay in the new account; it is rebuilt live.
- `azure-table:stknowledgeengine3v9/kefindfiches` (data): REBUILDABLE FOR FREE by the first kecore run or tickets run. Do not migrate.
- `azure-table:stknowledgeengine3v9/diagsessions, diagwriteback, convindex, convturns, itsmtickets` (data): Do not migrate: personal free text, unreachable from the new tenant, or demo data that rebuilds for free.
- `azure:stknowledgeengine3v9 demo containers kb-clienta/b/c, kecore-clienta/b/c, tickets-client{a,b,c}` (data): REBUILDABLE FOR FREE from git. Do not migrate.

### docs
- `docs/operations-runbook.md §10 (10.1-10.10, 10.5ter)` (docs): History of a pipeline that now works. Keep 5 or 6 gotcha lines (ARM-account flow, accountId vs name, region code, Owner role, token expiry, Default preset is enough) and point to the template comments for detail.
- `docs/operations-runbook.md §14` (docs): Pure history; the new repo starts without that lineage.
- `infra/deploy-tenants.local.json` (data): Old-tenant data and a pattern now built into the scripts.
- `scripts/repair-transcribed-flag.ps1, scripts/backfill-pii-pipeline.ps1, scripts/check-transcribed-total.ps1` (operator_tooling): Decision (b): one-off repair scripts for past incidents.
- `demo/ (build_demo.py, index.html)` (dead): Not part of the deployed system. Remove it unless the owner wants an offline showcase; if removed, drop the CLAUDE.md exception too.

### webapp
- `app/app.py (classic RAG assistant: lines 1-116 header, 169-196, 265-1022, 1063-1195, 1206-1223)` (legacy_but_wired): A new subscription has no saved conversations to show; removing it drops ~1000 lines, two tables, the markdown/nh3 dependencies and the /classic link.
- `app/diag_tab.py (POST /api/servicenow/webhook, POST /diag/internal/sweep) + scripts/enable-diagnostic-webhook.ps1` (dead): Disabled, never used, and its setup writes a secret to the operator machine (against CLAUDE.md). It also needs Easy Auth excludedPaths that webapp.bicep would wipe on redeploy.
- `config/engine.clienta.yaml, config/engine.clientb.yaml, config/engine.clientc.yaml` (data): They are RAG-era demo clients with no deterministic map, and their tenant and group IDs belong to the old environment. Keep one only if the owner wants a synthetic showcase client rebuilt through kecore.
- `demo/ (build_demo.py, index.html)` (dead): RAG-era showcase with no link to the deterministic engine, and never deployed.

### engine
- `kecore/pending.py` (tests_only): Move the constant into dictionary_service.py and delete the module and its test.
- `kecore/cli.py, kecore/__main__.py` (dead): CLAUDE.md forbids engine data on an operator machine. Spec slice 7 (docs/v10-deterministic-engine.md:113) already plans this removal.
- `kefind/{engine,compose,decide,graph_filter,calibration,understand}.py` (dead): Superseded by kefind.funnel + kefind.route, never run in Azure, and it writes to clients-local.
- `kefind/io.py` (tests_only): Move load_decomposed_jsonl into kefind/tests/helpers.py for test_funnel.
- `kefind/cli.py, kefind/__main__.py` (dead): Local-writing CLI on a superseded pipeline.
- `scoreboard/cli.py, scoreboard/__main__.py, scoreboard/importers.py, scoreboard/labeling.py, scoreboard/ids.py` (dead): Operator-machine workflow forbidden by CLAUDE.md, superseded by the Function and the Web App.
- `scoreboard/engines/search_baseline.py` (dead): Baseline for the old RAG, run locally. Not part of the deterministic platform.

### infra
- `infra/deploy-tenants.local.json` (dead): It holds stale tenant ids of an abandoned tenant and is not used by any deployment path.
- `infra/modules/appsettings-merge.bicep` (live): It is not needed once webapp.bicep owns every setting. It exists only because settings are split across templates.
- `infra/modules/videoindexer.bicep` (legacy_but_wired): The deterministic engine never consumes video transcripts. VI indexing was also re-billed three times by a past bug (runbook 12.4).
- `ingestion/audio-transcribe/` (legacy_but_wired): It does not serve the deterministic engine. It is key-based (speech-key), the most bug-prone pipeline (runbook §9 and 12.4), and costs Speech and Language usage.
- `ingestion/video-index/` (legacy_but_wired): The deterministic engine does not consume it.
- `scripts/backfill-pii-pipeline.ps1, scripts/check-transcribed-total.ps1, scripts/clear-transcribed-flag.ps1, scripts/rep…` (dead): They are historical data fixes for a pipeline the deterministic platform drops. They are written in French, against the repo-language rule.

