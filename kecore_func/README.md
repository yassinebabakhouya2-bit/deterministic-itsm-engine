# kecore_func: the V10 KB decomposition, fiche finder and scoreboard, run in Azure

Function App `fn-kecore-<prefix>-v9` (infrastructure: `infra/modules/kecore.bicep`, slice 1).
It runs `kecore` (slice 2), `kefind` (slice 3, in the live Diagnostic from slice 5) and `scoreboard`
(slice 4) unchanged; only where documents, tickets, labels, results and the LLM record live changes.

## Decompose a KB (slice 2)

```
POST /api/kecore/runs?code=<function key>
{"client": "client-s", "source_prefix": "Kbs/", "mode": "replay" | "record",
 "batch_size": 10, "limit": null, "with_dictionary": true}
```

One Durable Functions run: `extract` → `profile` → `decompose` (batches in parallel, 4 at a
time, see `host.json`) → `report`. The answer carries the status URLs; the run's output is the
summary (fiches, guided, citable, info_only, steps, steps_verified, mean_agreement, llm).

| Reads | Writes (`kecore-<client>`) |
| --- | --- |
| `kb-<client>/<source_prefix>*` (.docx, .pdf, .md, .txt, .html) | `runs/<run_id>/fiches.jsonl`, `profile.json`, `decomposed/<index>.json`, `fiches.decomposed.jsonl`, `report.md`, `summary.json`, `graph.json` (slice 3); `latest.json` |
| `kecore-<client>/llm-cache/` (record) | `llm-cache/` new answers, mode `record` only |

- `replay` never calls the model: an answer missing from the record is an error. It is the
  parity test. The dictionary call is the exception: when it is not in the record, the run falls
  back to the trigger-word dictionary and goes on.
- `record` reads the record and calls the model only for what it does not hold.
- A client missing from the `KECORE_CLIENTS` app setting is refused.
- The client's dictionary (`profile.json`, listed in `report.md`): the LLM picks the products
  among the names the code found in the document names and fiches, the code keeps only the
  spellings the fiches write. An entry that is not a product is rejected for good in
  `kecore-<client>/dictionary-decisions.json`: `{"rejected": ["<entry id>", ...]}`.
- Identity: the Function's managed identity for blob storage and Azure OpenAI; no key.

## Find the fiche for a ticket (slice 3)

```
POST /api/kecore/find?code=<function key>
{"client": "client-s", "text": "<ticket>", "answers": ["app:teams"], "run_id": null, "interpret": true,
 "observe": null}
```

The decision is code: `kefind.funnel` on the KB map of a run (`fiches.decomposed.jsonl`,
`profile.json` for the client's dictionary, `graph.json`; for a run made before slice 3 the
graph is rebuilt by the same code). Without `run_id`, `latest.json` names the run. `answers`
carries the entities the technician picked in answer to a previous question. `interpret`
(default true): one model call turns the ticket into search terms, English and French, checked
by code (`kefind.interpret`); they only rank. Recorded in `kecore-<client>/find-cache/` under the
hash of the request, so the same ticket gets the same terms; `false` gives the code alone. The
answer:

```
{"client", "run_id", "interpreted": true | false | null,
 "decision": {"kind": "fiche" | "question" | "abstain", "reason", "fiche_id", "fiches", "score",
              "question", "asks", "options", "trace"},
 "fiche": {"fiche_id", "label", "title", "status", "steps": [...], "prerequisites", "references",
           "duplicates"} | null,
 "candidates": [{"fiche_id", "label"}]}
```

`interpreted`: null when the request did not ask for the interpretation, false when it asked and
the model failed (the decision then rests on the ticket's own words: the Diagnostic does not use a
`text_only` decision made that way). Every JSON answer is ASCII with `charset=utf-8` (Windows
PowerShell 5.1 read a bare `application/json` as ISO-8859-1).

The client's calibrated settings apply when `kecore-<client>/funnel-config.json` exists (written
only by `POST /api/kecore/funnel-config/apply`, below): with a floor (`min_show`), a fiche scored
under it is offered first in a choice instead of shown alone; a fiche the ticket designates itself
(cited number, the technician's own answer) is always shown. The steps are the fiche's own text, verified at decomposition. The ticket text is neither logged
nor stored; only the model's search terms are recorded. A run's map is read once and kept in memory (4 maps at most): a run's folder never
changes once written. 400 for a bad request (unknown client included), 404 when the client has
no run yet.

## Real tickets and their scoreboard (slice 4)

```
POST /api/kecore/tickets/scrub?code=<key>    {"client": "client-s"}
POST /api/kecore/tickets/rescrub?code=<key>  {"client": "client-s"}
POST /api/kecore/tickets/runs?code=<key>     {"client": "client-s", "run_id": null, "interpret": true, "limit": 500}
POST /api/kecore/scoreboard/runs?code=<key>  {"client": "client-s", "run_id": null, "interpret": true, "max_wrong": 0.05}
GET  /api/kecore/scoreboard/latest?client=client-s&code=<key>
POST /api/kecore/funnel-config/apply?code=<key>  {"client": "client-s", "scoreboard_id": "<id>"} | {"client": "client-s", "reset": true}
```

- `scrub`: every `tickets-<client>/raw/*.csv` (an ITSM export dropped there, never in SharePoint)
  is parsed by `kecore.tickets.scrub` -- person columns dropped, every other column but the
  structured ones cleaned (e-mail signature cut, name after a greeting, e-mail, phone, @mention) --
  written one row per ticket to the `tickets` table, and the raw export deleted. `rescrub`
  re-applies the current cleaning to the rows already stored.
- `tickets/runs` (Durable): refreshes the fiche catalog the labeling tab offers (`kefindfiches`),
  then every ticket goes through the funnel as `/find` would run it; each finding is merged into
  the ticket's row (`kefind_*`) and the run tallies kinds and reasons. A ticket that fails is
  counted (`errors`, reason `error:<type>`), never fatal.
- Labels: written by the Web App's labeling tab (`/labels`, `app/labels.py`) to `ticketlabels`.
- `scoreboard/runs` (Durable): freezes the labeled set (`scoreboard/<id>/dataset.jsonl`), replays
  it through `kefind.funnel_engine.FunnelEngine` with the floor off, and reports with the
  `scoreboard` package (`report.md`, `summary.json`, a row in `kecorescores`). The floor is chosen
  on half the labels (split by a hash of the ticket id) and confirmed on the other half.
- `funnel-config/apply`: refused (409) unless the run's floor was confirmed on both halves; the
  previous `funnel-config.json` is kept under `funnel-config.history/`.

## The engine in the Diagnostic and the dictionary review (slice 5)

```
GET  /api/kecore/fiche?client=client-s&fiche_id=KB0120[&run_id=...]&code=<key>
GET  /api/kecore/dictionary?client=client-s&code=<key>
POST /api/kecore/dictionary/decision?code=<key>
     {"client": "client-s", "term": "coupa", "accept": true, "canonical": null, "by": "<name>"}
     {"client": "client-s", "entry": "<entry id>", "accept": false, "by": "<name>"}
```

- The Web App's Diagnostic calls `/find`, then `/fiche` for the fiche it guides with: its verified
  steps, its neighbours and its own text, from the run that decided (`run_id`), so a session keeps
  the same map after a new kecore run (`orchestration/guide/kefind_ports.py`). The Web App holds
  the key through a Key Vault reference (`infra/modules/kecore-link.bicep`).
- `/find` accepts `"observe": "<hash of the asking session>"` (16 to 64 hexadecimal characters):
  the question's product-like names unknown to the dictionary (`l'application X`, 3 words and 40
  characters at most) are counted once per distinct session in the table `kefindpending`; below 3
  sessions only a hash of the name is stored, never the question. Observation never fails the answer.
- `/dictionary`: the current entries, the names seen in at least 3 sessions (`ready`), how many are
  still watched, the decisions. `/dictionary/decision` records one decision, once (404: unknown
  or not ready yet; 409: already decided); the next `POST /api/kecore/runs` reads them with the
  hand-written `dictionary-decisions.json` (`kecore_pipeline.profile`).

## The semantic index of a run (2026-10-09)

`POST /api/kecore/runs` builds, after `report` and before `latest.json` (`publish`, last), the run's
`semantic/` folder: `cards/` (how people ask for each fiche, written once by the model, checked by
code), `heldout/` (exam questions, never indexed), `index.json` + `vectors.f32` (frozen, with their
sha256) and `calibration.json` (thresholds chosen on the KB itself). `"semantic": false` skips it.
Model answers are recorded in `llm-cache/`, vectors in `embed-cache/`: a `replay` run rebuilds the
folder byte for byte with no call. `/find` then decides by meaning (`"mode": "semantic"`), the
question's vector recorded in `find-cache/` (never its text); without a vector it decides by words
and says so (`"mode": "degraded"`). Details: runbook §19.11, `semantic_service.py`.

Code: `function_app.py` (Durable and HTTP wiring only), `kecore_pipeline.py` (the steps, no
Azure SDK, tested in memory), `kefind_service.py` (the find request, no Azure SDK, tested in
memory), `tickets_service.py` and `scoreboard_service.py` (slice 4, no Azure SDK, tested in
memory), `dictionary_service.py` (slice 5, no Azure SDK, tested in memory), `kecore_blob.py` (blob storage), `kecore_table.py` (table storage). Tests, from the repository root:

```powershell
python -m unittest discover -s kecore_func/tests
```

Deployment: `scripts/deploy-kecore-function.ps1` (ships `kecore_func`, `kecore`, `kefind` and
`scoreboard`); procedure and parity check: runbook §16; the finder: runbook §17; tickets, labels
and scoreboard: runbook §18; the engine in the Diagnostic, the dictionary review and the
write-back: runbook §19.
