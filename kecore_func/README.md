# kecore_func: the V10 KB decomposition, run in Azure

Function App `fn-kecore-<prefix>-v9` (infrastructure: `infra/modules/kecore.bicep`, slice 1).
It runs `kecore` unchanged; only where documents, results and the LLM record live changes.

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
| `kb-<client>/<source_prefix>*` (.docx, .pdf, .md, .txt, .html) | `runs/<run_id>/fiches.jsonl`, `profile.json`, `decomposed/<index>.json`, `fiches.decomposed.jsonl`, `report.md`, `summary.json`; `latest.json` |
| `kecore-<client>/llm-cache/` (record) | `llm-cache/` new answers, mode `record` only |

- `replay` never calls the model: an answer missing from the record is an error. It is the
  parity test.
- `record` reads the record and calls the model only for what it does not hold.
- A client missing from the `KECORE_CLIENTS` app setting is refused.
- Identity: the Function's managed identity for blob storage and Azure OpenAI; no key.

Code: `function_app.py` (Durable wiring only), `kecore_pipeline.py` (the steps, no Azure SDK,
tested in memory), `kecore_blob.py` (blob storage). Tests, from the repository root:

```powershell
python -m unittest discover -s kecore_func/tests
```

Deployment: `scripts/deploy-kecore-function.ps1`; procedure and parity check: runbook §16.
