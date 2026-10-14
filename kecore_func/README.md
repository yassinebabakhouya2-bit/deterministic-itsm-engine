# kecore_func: the V10 engine run in Azure

Function App `fn-kecore-<prefix>-v9` (infrastructure: `infra/modules/kecore.bicep`, slice 1). It runs
`kecore` (KB decomposition, slice 2), `kefind` (the fiche finder, slice 3; in the live Diagnostic from
slice 5; the deterministic routing since the routing chantier) and `scoreboard` (slice 4) unchanged:
only where documents, tickets, labels, decisions, results and the model records live changes. Every
service module (`*_service.py`, `kecore_pipeline.py`) is free of Azure SDKs and tested in memory;
`function_app.py` is the HTTP and Durable wiring only.

## Routes

Every route takes the function key (`?code=<key>`). Every JSON answer is ASCII with
`application/json; charset=utf-8` (Windows PowerShell 5.1 read a bare `application/json` as
ISO-8859-1, runbook 19.8). A client missing from the `KECORE_CLIENTS` app setting is refused with 400,
without listing the others.

| Route | Body / query | Answer |
| --- | --- | --- |
| `POST kecore/runs` | `{"client", "source_prefix", "mode", "batch_size", "limit", "with_dictionary", "semantic", "enrich", "llm_parallelism", "routing"}` | 202, Durable `kecore_run` (below) |
| `POST kecore/find` | `{"client", "text", "answers", "run_id", "interpret", "observe"}` | the decision (below) |
| `GET kecore/fiche` | `?client=&fiche_id=[&run_id=]` | a fiche's verified steps, neighbours, text; 404 unknown |
| `GET kecore/dictionary` | `?client=` | the dictionary review (slice 5) |
| `POST kecore/dictionary/decision` | `{"client", "term"\|"entry", "accept", "canonical", "by"}` | 200; 404 not ready; 409 already decided |
| `POST kecore/tickets/scrub` | `{"client"}` | raw exports scrubbed into `tickets` |
| `POST kecore/tickets/rescrub` | `{"client"}` | the current cleaning re-applied to stored rows |
| `POST kecore/tickets/runs` | `{"client", "run_id", "interpret", "limit"}` | 202, Durable `tickets_run` |
| `POST kecore/scoreboard/runs` | `{"client", "run_id", "interpret", "max_wrong", "llm_parallelism"}` | 202, Durable `scoreboard_run` |
| `GET kecore/scoreboard/latest` | `?client=` | the last scoreboard: summary and report.md; 404 none yet |
| `POST kecore/funnel-config/apply` | `{"client", "scoreboard_id"}` or `{"client", "reset": true}` | the new `funnel-config.json`; 409 refused |
| `GET kecore/version` | | `{"commit"}`: the commit packaged (`BUILD_COMMIT`, written by the deploy script) |
| `POST kecore/routing/runs` | `{"client", "mode"?, "measure"?, "llm_parallelism"?, "batch_size"?}` | 202, Durable `routing_run` (instance id = build id); 409 while a routing run holds the lease |
| `GET kecore/routing` | `?client=` | routing status: taxonomy, latest build, run in progress, candidate, active, gate, `effective`, `killed`, `why_not_effective` |
| `POST kecore/routing/activate` | `{"client", "build_id", "by", "accept_insufficient"?, "reason"?}` | 200 `{"active"}`; 409 refused (why, in the message) |
| `POST kecore/routing/deactivate` | `{"client", "by"}` | 200 `{"active": null, "killed": true}` |
| `GET kecore/review` | `?client=[&status=]` | the review overview (counts stay global; an unknown status filter is ignored) |
| `GET kecore/review/fiche` | `?client=&key=` | one fiche to review; 400 bad key; 404 not a routable fiche |
| `GET kecore/review/next` | `?client=&status=proposed\|stale` | `{"key": <next fiche key> \| null}` |
| `POST kecore/review/decision` | `{"client", "by", "by_id"?, "fiche_id", "source_sha256", "seen", "status", "intent", "primary_app", "aliases", "codes", "note"}` | 200 `{"decision", "collisions"}`; 400 French message; 404; 409 changed since shown |
| `POST kecore/review/carry` | `{"client", "by", "by_id"?, "keys": [1..500 fiche keys]}` | 200 `{"carried": n, "failed": [{"key", "error"}]}` |
| `POST kecore/review/taxonomy` | `{"client", "by", "source_build", "intents": [{"id", "label_fr", "description_fr"}], "orphans_ok"?}` | 200 `{"version", "sha256", "intents", "orphans"}`; 400 French message naming the row; 409 `{"error", "orphans"}` or a concurrent freeze |

The review routes answer 404 while the client has no published run; the review and routing routes
answer 500 naming the file when a stored file no longer reads as written (a hand-edited `taxonomy.json`
fails its sha256).
The routes the Web App calls are wrapped by `app/kecore_client.py`; the "Revue" tab is `app/review_tab.py`.

## Orchestrations and activities

Durable Functions (`host.json`: hub `kecorehub`, at most 4 activities and 4 orchestrations at a time,
`functionTimeout` 2 h). Batches that may call the model run through `_fan_out`: `llm_parallelism`
(1-8, default 2) batches at a time in a `record` run, all at once in `replay`, and an input scheduled
before that key existed keeps the shape its history holds (all at once).

| Orchestration | Activities, in order |
| --- | --- |
| `kecore_run` | `kecore_extract` → `kecore_profile` → `kecore_decompose` (batches) → `kecore_report` → [`"semantic"`: `kecore_semantic_plan` → `kecore_cards` (batches) → `kecore_heldout` (batches) → `kecore_semantic_index` → `kecore_calibrate`] → [`"enrich"`: `kecore_semantic_plan` → `kecore_enrich` (batches) → `kecore_enrich_summary`] → [`"routing"`: the routing steps below, without the measure] → `kecore_publish` → [`"catalog"`: `kecore_catalog`] |
| `routing_run` | `routing_prepare` → [`routing_taxonomy`] → [`routing_assign` (batches) → `routing_assign_summary`] → `routing_build` → [`"measure"`: `scoreboard_prepare` → `scoreboard_batch` (batches) → `scoreboard_report`] → `routing_finish` (always, even after a failure) |
| `tickets_run` | `tickets_count` → `tickets_catalog` → `tickets_run_batch` (batches, on one map) |
| `scoreboard_run` | `scoreboard_prepare` → `scoreboard_batch` (batches) → `scoreboard_report` |

A failed semantic folder, enrichment, routing step or catalog never fails a kecore run: the run is
still published and its summary says why. Orchestrator code reads no clock: times are taken by the
activities and routes (`_now`).

## A kecore run: decompose, enrich, route, publish

```
POST /api/kecore/runs?code=<function key>
{"client": "client-s", "source_prefix": "Kbs/", "mode": "replay" | "record", "batch_size": 10, "limit": null,
 "with_dictionary": true, "semantic": true, "enrich": true, "llm_parallelism": 2, "routing": true}
```

- `extract` → `profile` → `decompose` → `report`: the KB map (`fiches.decomposed.jsonl`, `graph.json`,
  `report.md`, `summary.json`). The client's dictionary (`profile.json`, listed in `report.md`): the model
  picks the products among the names the code found, the code keeps only the spellings the fiches write;
  an entry rejected for good goes to `dictionary-decisions.json` (`{"rejected": [...]}`), and the
  dictionary review's decisions (`kefindpending`) join the run when `with_dictionary` is true.
- Exclusion (rule 3, runbook 19.14): reuse placeholders and empty fiches stay out of the map, listed in
  `runs/<run>/excluded.json` and `report.md`. `kecore-<client>/exclusion-config.json`
  (`{"title_patterns", "min_chars", "force_include"}`) is UTF-8, with or without a BOM; UTF-16, UTF-32
  and the ANSI code page fail the run ("save it as UTF-8"). A `force_include` entry names a fiche id
  exactly, or the one fiche equal to it once spaces are collapsed and case ignored; `forced`,
  `matched_loosely` and `force_include_unmatched` are reported in `excluded.json`, the run's summary and
  `report.md`.
- `semantic` (default true): the run's `semantic/` folder (below).
- `enrich` (default true): pass A of the enrichment (`enrichment_service.py`, `kefind.enrich`), one
  proposal per ranked fiche under `runs/<run>/enrichment/` (intent, application with evidence, aliases),
  checked by code. Proposals only: nothing routes on them unless a person validates it.
- `routing` (default true, strictly a JSON bool -- PowerShell's `"false"` is refused): the routing run's
  steps (build id = run id, `kind` "kecore") on the NEW run, before it is published, without the
  measure. When a person activated tables and the new tables answer exactly as they do (same
  `content_sha256` and `kb_sha256`), the activation is carried over to the new run the moment it is
  published (below). `"routing": false` skips it: the new run is then served without routing.
- `publish`: `latest.json`, last of all (and the run's `published.json`): the run's folder never changes
  afterwards. Then `catalog` (`kefindfiches`): the fiches the labeling tab offers follow the published map.
- `mode`: `record` reads the record (`llm-cache/`, `embed-cache/`) and calls the model only for what it
  does not hold; `replay` never calls it -- a missing answer is an error (the dictionary call falls back
  to the trigger-word dictionary). A replay rewrites the run's folders byte for byte.
- The run's output: fiches, guided, citable, info_only, steps, steps_verified, mean_agreement, llm,
  dictionary, graph, exclusion, run_id, semantic, enrichment, routing (the routing step's build, tables,
  carried / why_not) -- all of them in `latest.json`'s summary too -- and catalog (refreshed after publish).

## Find the fiche for a ticket

```
POST /api/kecore/find?code=<function key>
{"client": "client-s", "text": "<ticket>", "answers": ["app:teams"], "run_id": null, "interpret": true,
 "observe": null}
```

The decision is code (`kefind_service.respond` → `kefind.route.answer`, the one router live `/find`, the
scoreboard and the tickets run share) on the KB map of a run: `latest.json`'s, or `run_id` (a
Diagnostic session keeps the map that decided). The answer:

```
{"client", "run_id", "mode": "semantic" | "words" | "degraded" | "routed", "semantic_error",
 "interpreted": true | false | null,
 "decision": {"kind": "fiche" | "question" | "abstain", "reason", "fiche_id", "fiches", "score",
              "question", "asks", "options", "trace", "version", "designated", "degraded"},
 "fiche": {"fiche_id", "label", "title", "status", "steps": [...], "prerequisites", "references",
           "duplicates"} | null,
 "candidates": [{"fiche_id", "label"}],
 "route": {"version", "level": "L0".."L4", "rule", "key", "fiche_id", "tables_sha256", "content_sha256",
           "build_id", "run_id", "l0", "l1", "l2"} | null,
 "routing_error": null | "<why tables that exist were not used>"}
```

- Without active routing tables (the default until a person activates some), the answer is today's,
  exactly, with `"route": null`: with a semantic index the question is embedded once (recorded in
  `find-cache/`, never its text) and decided by meaning against the run's calibration; without one, or
  without a vector (`"mode": "degraded"`, `semantic_error` says why), the words decide, and `interpret`
  (default true) adds one recorded model call that turns the ticket into checked search terms.
- With tables a person activated FOR THIS RUN (and the kill switch off): L0 a fiche number or a
  validated error code, L1 a validated alias (at most one routing word more than the alias), L2 the
  validated (intent, application) through one recorded mapping call -- each shows ONE fiche, designated,
  `"mode": "routed"`, no embedding and no interpretation -- then L3 today's engine, whose fiches are
  OFFERED (never shown alone), and L4 nothing. `routing_error` says why existing tables were not used:
  kill switch, built for another run, sha256 / code version / knowledge base mismatch, an unreadable
  file, or `routing failed (<Exc>)` when a routed answer raised (it is then answered again without
  routing). The pointer and kill switch are re-read at most once a minute per instance (at once on the
  instance that activated or deactivated); tables are cached by sha256.
- The client's calibrated settings apply when `funnel-config.json` exists (written only by
  `POST kecore/funnel-config/apply`): a scored fiche under the floor (`min_show`) is offered, not shown;
  a fiche the ticket designates itself is always shown.
- `observe` (16-64 hexadecimal characters, a hash of the session): the question's product-like names
  unknown to the dictionary feed the dictionary review (`kefindpending`), never the text. It never fails
  the answer. 400 for a bad request; 404 when the client has no run yet.

## Real tickets and their scoreboard (slice 4)

- `tickets/scrub`: every `tickets-<client>/raw/*.csv` (an ITSM export, never in SharePoint) is parsed by
  `kecore.tickets.scrub` -- person columns dropped, every other free-text column cleaned -- written one
  row per ticket to `tickets`, and the raw export deleted. `rescrub` re-applies the current cleaning.
- `tickets/runs`: refreshes the catalog (`kefindfiches`), then every ticket goes through the router
  without tables, as `/find` would answer it; each finding is merged into the ticket's row (`kefind_*`,
  `kefind_degraded` "" unless the decision was degraded). A failing ticket is counted, never fatal.
- Labels and reference questions: written by the Web App's labeling tab (`ticketlabels`, `refquestions`).
- `scoreboard/runs`: freezes the labeled set and the reference questions (`dataset.jsonl`), replays them
  through `kefind.funnel_engine.FunnelEngine` with the floor off, and reports with the `scoreboard`
  package (`report.md`, `summary.json`, a row in `kecorescores`). The floor is chosen on half the labels
  and confirmed on the other half. When the candidate routing tables were built on the measured run, the
  same questions also go through them (`kefind-route`, same interpretation record and embedder, plus the
  L2 mapping record) and the report writes their gate (`routing/gates/`, below).
- Degraded decisions (no question vector, a vector from another model, a failed L2 call) are counted per
  engine: they were not made as `/find` makes them, so a scoreboard holding one cannot apply a floor and
  its routing gate is invalid.
- `funnel-config/apply`: refused (409) unless the floor was confirmed on both halves, on the current map
  (one that decides by words: a semantic map has its own calibration), with the interpretation, with no
  transient model failure and no degraded decision, for a ceiling of 0.10 or stricter; the previous file
  is kept under `funnel-config.history/`.

## The Diagnostic and the dictionary review (slice 5)

- The Web App's Diagnostic calls `/find`, then `/fiche` for the fiche it guides with, from the run that
  decided (`orchestration/guide/kefind_ports.py`). With a route, L0-L2 fiches are shown as identified by
  the deterministic engine; L2/L3 choices are offered; L4 opens the conversation. The Web App holds the
  key through a Key Vault reference (`infra/modules/kecore-link.bicep`).
- `/dictionary`: the current entries, the names seen in at least 3 sessions (`ready`), the decisions.
  `/dictionary/decision` records one decision, once; the next kecore run reads them.

## The semantic index of a run (2026-10-09)

After `report` and before `publish`, the run's `semantic/` folder: `cards/` (how people ask for each
fiche, written once by the model, checked by code), `heldout/` (exam questions, never indexed),
`index.json` + `vectors.f32` (frozen, with their sha256) and `calibration.json` (thresholds chosen on the
KB itself). Model answers are recorded in `llm-cache/`, vectors in `embed-cache/`: a replay rebuilds the
folder byte for byte with no call. Details: runbook §19.11, `semantic_service.py`.

## Routing: only what a person validated, measured and activated

Spec: `docs/v10-deterministic-engine.md`, "Semantic enrichment at ingestion, deterministic routing at
runtime". Code: `routing_service.py` (storage side), `kefind.review` (decisions), `kefind.routing` (tables,
matchers), `kefind.taxonomy` (pass B), `kefind.assign` (pass C), `kefind.route` (the router).

- Phase 1 needs neither a taxonomy nor pass C: L0 (each routable fiche's own number, validated error
  codes still unique) and L1 (aliases a person validated: a reference question promoted word for word,
  pass A/C proposals ticked, or typed). Phase 2 adds L2: a person freezes a taxonomy from pass B's
  proposal, pass C assigns each fiche one intent of it, and a person validates (intent, application).
- The routing run (`POST kecore/routing/runs`, or a kecore run's routing step): `routing_prepare` takes
  the lease (`routing/running.json`: one routing run at a time; a lease older than 2 h is a dead run's)
  and says which passes are needed. Pass B (`routing_taxonomy`) runs while no taxonomy is frozen, once
  per run, and is skipped while pass A has errors (a record kecore run makes only the missing calls; the
  L0/L1 tables are still built).
  Pass C (`routing_assign`, `routing_assign_summary`) runs with a frozen taxonomy when the run or the
  taxonomy changed, or it had errors. `routing_build` ALWAYS follows, from every stored decision (stale
  ones too: their previous aliases still block): a token set two fiches claim is a collision, a key a
  fiche under review proposes is blocked -- a partial review never makes a key unique. `"measure"`
  (default true) then runs the scoreboard (`sb_id` "<build>-sb"). `routing_finish` always runs: the
  build's `status.json`, `latest-build.json`, the lease released.
- The review (`GET kecore/review*`, `POST kecore/review/*`): a decision is keyed by the fiche and the
  sha256 of its EXTRACTED text (`runs/<run>/fiches.jsonl`, before boilerplate removal: adding other
  documents never sends a fiche back to review); an edited fiche becomes "stale" and its decision stops
  routing until a person decides again or carries it over (`/carry`, checked again in full). A decision
  is created, or merged into the row as the reviewer saw it (`seen`: its `decided_at`; a row changed
  meanwhile is a 409). Refusals shown to the reviewer are French. A freeze that takes their intent from
  validated fiches is refused (409 with them) unless `orphans_ok`.
- The gate (`routing/gates/<content_sha256>-<kb_run_id>.json`, written by the scoreboard's report):
  `passed` (no wrong fiche shown at L0-L2, the right fiche first at least as often as today's engine, at
  least 30 compared questions); `insufficient` (too few questions, or nothing routed exactly); `failed`
  (a wrong fiche shown at L0-L2, or the right fiche first less often); `invalid` (an error, a transient
  model failure, a degraded decision, the two engines not on the same questions). Reference questions a reviewer
  had on screen while writing the tables ("seen") are left out of the right-first comparison, never out
  of the wrong-exact count.
- Activation (`POST kecore/routing/activate`): refused (409) unless the build's tables load and verify,
  were built on the run `/find` serves now, and their gate on that run passed -- or was `insufficient`
  and the person accepts it with a reason, recorded as `override`. It clears the kill switch: only a
  person's activation does. The previous pointer is kept under `routing/history/`.
- Carry-over: a build whose tables have the same `content_sha256` AND `kb_sha256` as the active ones
  moves `active.json` to itself with no new measure, the old entry kept as `previous` (sessions pinned on
  the earlier run keep being served), unless the kill switch is on.
- Deactivation (`POST kecore/routing/deactivate`): the sticky kill switch (`routing/kill.json`, written
  first), then the pointer moved to `routing/history/`. Every `/find` stops routing within a minute, at
  once on the instance that handled it; no rebuild, kecore run or carry-over turns it back on.

## Storage

Blob containers: `kb-<client>/` (the documents, read only), `tickets-<client>/raw/` (ITSM exports,
deleted once scrubbed), and `kecore-<client>/`:

| Path | Written by | What |
| --- | --- | --- |
| `latest.json` | `kecore_publish` | the run `/find` serves, and its summary |
| `runs/<run>/fiches.jsonl`, `profile.json`, `decomposed/`, `fiches.decomposed.jsonl`, `graph.json`, `excluded.json`, `report.md`, `summary.json`, `published.json` | kecore run | the KB map of a run |
| `runs/<run>/semantic/` | kecore run | cards, exam, index, vectors, calibration |
| `runs/<run>/enrichment/<nnnnn>.json`, `summary.json` | kecore run | pass A, one proposal per ranked fiche |
| `llm-cache/`, `embed-cache/` | runs | the model and embedding records of runs (passes A, B, C included) |
| `find-cache/` | `/find`, tickets run, scoreboard | interpretation, question vectors and L2 mappings, shared by live answers and measures |
| `dictionary-decisions.json`, `exclusion-config.json` | a person | hand-written settings (UTF-8) |
| `funnel-config.json`, `funnel-config.history/` | `funnel-config/apply` | the calibrated floor |
| `scoreboard/latest.json`, `scoreboard/<id>/dataset.jsonl`, `results/`, `results.jsonl`, `results-route/`, `results-route.jsonl`, `report.md`, `summary.json` | scoreboard | one measure |
| `taxonomy.json`, `taxonomy.history/v<n>.json` | `POST kecore/review/taxonomy` | the frozen taxonomy (never edited by hand: its sha256 is checked) |
| `routing/builds/<build>/status.json`, `taxonomy.json`, `taxonomy.proposed.json`, `assign/<nnnnn>.json`, `assign/index.json`, `assign/summary.json`, `decisions.json`, `tables.json`, `review.json`, `summary.json` | routing run | one build: the taxonomy copy it used, passes B and C, the decisions it was built from, the tables |
| `routing/taxonomy-proposal.json`, `routing/assignments.json` | routing run | the builds holding the last pass B proposal and the pass C proposals |
| `routing/latest-build.json`, `routing/candidate.json`, `routing/running.json` | routing run | the last build, the tables the next measure and activation are about, the lease |
| `routing/gates/<content_sha256>-<kb_run_id>.json` | scoreboard report | the gate of those tables on that run |
| `routing/active.json`, `routing/kill.json`, `routing/history/` | activate, deactivate, carry-over | what `/find` serves, the kill switch, every earlier pointer |

Tables (one partition per client; `kecore_table.py`): `tickets`, `kefindfiches`, `ticketlabels`,
`refquestions`, `kecorescores` (one row per scoreboard, the gate's columns included: `route_status`,
`route_build`, `route_wrong_k`, `route_wrong_n`, `route_first_k`, `base_first_k`, `route_compared`,
`degraded_k`), `kefindpending`, `kefindreview` (one row per fiche and source text a person decided on;
created on first use).

## Settings and model budgets

App settings: `KECORE_CLIENTS` (the clients served), `KECORE_BLOB_ENDPOINT`, `KECORE_TABLE_ENDPOINT`
(optional: derived from the blob endpoint), `KECORE_TICKETS_TABLE` (optional), `KECORE_AOAI_ENDPOINT`,
`KECORE_AOAI_DEPLOYMENT`, `KECORE_AOAI_API_VERSION` (default 2024-10-21),
`KECORE_AOAI_EMBEDDING_DEPLOYMENT` (default text-embedding-3-large), `KECORE_AOAI_BATCH_DEPLOYMENT`
(optional: the deployment of passes B and C only, away from the live answers' quota). Identity: the
Function's managed identity for blobs, tables and Azure OpenAI; no key.

| Calls | Budget |
| --- | --- |
| kecore run, tickets run, scoreboard | 12 retries after a 429, each waiting what the service asks |
| live `/find` (interpretation, vector) | 25 s read timeout, 2 retries, 2 retries after a 429 |
| pass B | `max_tokens` 12000, 600 s, 1 retry |
| pass C | `max_tokens` 800 |
| L2 mapping, live `/find` | `max_tokens` 150, 8 s, no retry (a slow mapping gives the question to L3, marked degraded) |
| L2 mapping, scoreboard | `max_tokens` 150, the batch budget |

## Code and tests

`function_app.py` (HTTP and Durable wiring only), `kecore_pipeline.py` (the kecore run's steps),
`semantic_service.py` (the semantic folder), `enrichment_service.py` (pass A), `routing_service.py`
(passes B and C in a routing run, the review, the tables, activation and kill switch),
`kefind_service.py` (the find request), `tickets_service.py` and `scoreboard_service.py` (slice 4, the
routed measure and its gate), `dictionary_service.py` (slice 5), `kecore_blob.py` (blob storage),
`kecore_table.py` (table storage). Tests, from the repository root:

```powershell
python -m unittest discover -s kecore_func/tests
```

`tests/test_routing_end_to_end.py` drives the whole routing chain through the Function's own routes,
orchestrators and activities, in memory, on the demo KB: a kecore run with pass A and pass B, a review
decision, a measure, an activation with an override, `/find` at L0, L1 and L3, a frozen taxonomy with
pass C and an L2 answer, a carry-over to a later run, a changed KB, the kill switch, a semantic run.
`test_function_app.py` and `test_routing_end_to_end.py` are skipped when `azure-functions`,
`azure-functions-durable` or `azure-data-tables` is not installed.

Deployment: `scripts/deploy-kecore-function.ps1` (ships `kecore_func`, `kecore`, `kefind` and
`scoreboard`, and waits until `GET kecore/version` answers the commit it shipped); procedure and parity
check: runbook §16; the finder: §17; tickets, labels and scoreboard: §18; the Diagnostic, the dictionary
review, the write-back, the semantic index, pass A and the safe run blocks: §19.
