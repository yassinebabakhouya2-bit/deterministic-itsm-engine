# kecore_func: the V10 KB decomposition and fiche finder, run in Azure

Function App `fn-kecore-<prefix>-v9` (infrastructure: `infra/modules/kecore.bicep`, slice 1).
It runs `kecore` (slice 2) and `kefind` (slice 3) unchanged; only where documents, results and
the LLM record live changes.

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
{"client": "client-s", "text": "<ticket>", "answers": ["app:teams"], "run_id": null, "interpret": true}
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
{"client", "run_id",
 "decision": {"kind": "fiche" | "question" | "abstain", "reason", "fiche_id", "fiches", "score",
              "question", "asks", "options", "trace"},
 "fiche": {"fiche_id", "label", "title", "status", "steps": [...], "prerequisites", "references",
           "duplicates"} | null,
 "candidates": [{"fiche_id", "label"}]}
```

The steps are the fiche's own text, verified at decomposition. The ticket text is neither logged
nor stored; only the model's search terms are recorded. A run's map is read once and kept in memory (4 maps at most): a run's folder never
changes once written. 400 for a bad request (unknown client included), 404 when the client has
no run yet.

Code: `function_app.py` (Durable and HTTP wiring only), `kecore_pipeline.py` (the steps, no
Azure SDK, tested in memory), `kefind_service.py` (the find request, no Azure SDK, tested in
memory), `kecore_blob.py` (blob storage). Tests, from the repository root:

```powershell
python -m unittest discover -s kecore_func/tests
```

Deployment: `scripts/deploy-kecore-function.ps1` (ships `kecore_func`, `kecore` and `kefind`);
procedure and parity check: runbook §16; the finder: runbook §17.
