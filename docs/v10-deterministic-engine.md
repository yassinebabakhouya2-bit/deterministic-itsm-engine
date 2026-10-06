# The deterministic diagnostic engine (kecore / kefind / scoreboard)

A second engine track in this repository, alongside the showcase RAG
described in [`architecture.md`](architecture.md). Where the showcase engine
reaches determinism through model settings (`temperature=0`, fixed seed), this
track pushes the same goal one level further: **the code decides, never the
LLM and never the search index.** The LLM is restricted to two jobs —
understanding input and drafting an already-chosen answer — and every step
shown to a technician is a citation verified verbatim against the source
fiche, algorithmically, not asserted.

This is not a rewrite of the showcase engine; it is a harder, measured
version of the same diagnostic problem, built incrementally with a
measurement harness first so every later change is judged against a number,
not an impression. It is not a separate product either: as of 2026-10-05 it
lives in the same repository as the diagnostic engine and the ITSM action
engine (`deterministic-itsm-engine`, formerly `knowledgeengine-rag-platform`)
— all three are the same "code decides" principle applied to a different
decision (which fiche, which step, which ITSM action).

## The three packages

| Package | Role |
|---|---|
| [`scoreboard/`](../scoreboard/README.md) | The measurement harness, built first. Replays labeled tickets through an engine, reports exact-fiche@1, recall@5, abstention correctness, stability, latency and cost, each with a Wilson interval; compares engines on the same tickets with an exact McNemar test. Nothing below ships without moving these numbers. |
| [`kecore/`](../kecore/README.md) | Decomposes a client's KB fiches into verified steps. A double decomposition (LLM + rule-based) that only trusts a quote when it is found verbatim in the fiche (`kecore.text.NormalizedText`); confidence comes from self-consistency (the LLM checked against a second independent pass), not from LLM-vs-rules agreement, which only measured a client's writing style. |
| [`kefind/`](../kefind/README.md) | A 5-step deterministic RAG: understand (LLM, code validates), search (BM25 + embeddings + entity bonus), filter by graph (not built yet), decide (code, fiche / question / abstain), compose (LLM drafts, code verifies every quote). |

## Three non-negotiable principles

1. The LLM never plans steps and never executes code — it is restricted to
   parsing intent into closed states (`YES`, `NO`, `EXPLAIN`, `IMPOSSIBLE`,
   `PERMISSION_DENIED`) and to drafting already-decided content.
2. Navigation through a procedure is a state machine reading immutable,
   hashed (SHA-256) DAGs — not an LLM improvising the next step.
3. Every diagnostic step is anchored on a verbatim quote, checked
   algorithmically against the source text — the same mechanism as
   `kecore.text.NormalizedText`, never relaxed.

## Where it stands (October 2026)

A full audit of all three packages (code + tests) on 2026-10-01 found the
measurement harness and the decomposition pipeline solid, and three concrete
defects in the decision layer: a hard-coded, non-generic application/OS
dictionary (`kecore.entities.APPS`), a `TfidfEmbeddingProvider` documented as
a stand-in rather than real embeddings, and fixed decision thresholds
(`kefind.decide.Thresholds`) never calibrated on real data. Fixing these —
while keeping everything above generic across clients, never a branch for
one — is organized as eight dependency-ordered pillars:

| # | Pillar | Status |
|---|---|---|
| 1 | Structure induction (per-client archetype detection, query enrichment) | 🟢 Planned |
| 2 | Dynamic per-client entity dictionary (replaces `kecore.entities.APPS`) | 🟡 In progress — offline corpus scan (`kecore.profile.build_dictionary`) shipped; the online feedback loop's rules (`kecore.pending`, storage-agnostic, human-validated) are written and tested, its Azure side (Table + review tab, `observe` on live questions) is slice 5 below. Known gap: `kefind.understand` extracts ticket entities without the client's dictionary, so a client-specific app named in a ticket never earns the entity bonus — fixed in slice 5 |
| 3 | Knowledge-graph relations between fiches (replaces the no-op `kefind.graph_filter`) | 🟢 Planned — needs pillar 2 and a real embedding service |
| 4 | Canonical ingestion schema | 🟢 Planned |
| 5 | Bayesian step ordering (p/C ratio) | 🟢 Planned — needs pillar 2 |
| 6 | Conformal abstention (replaces fixed `kefind.decide.Thresholds`) | 🟢 Planned — calibration must use only validated tickets, never raw production ones |
| 7 | ServiceNow write-path guardrails (Pydantic + RBAC) | 🟢 Planned |
| 8 | Event sourcing / traceability | 🟢 Planned |

Dependency order: pillar 2 first (everything else needs a real entity
dictionary); then a real embedding service (prerequisite for pillars 3 and
5); then pillars 1 and 4; then pillar 3; then pillars 5–6; then 7–8.

## Azure-native migration (decided 2026-10-05)

Until 2026-10-05 the three packages ran as local Python CLIs writing to
`clients-local/`. Decision: all of V10 moves to Azure, provisioned in Bicep,
like the rest of the platform; nothing is created on an operator's machine.
The Python packages stay the engine (pure, tested logic); what changes is
where they run, where their inputs and outputs live, and who triggers them.

| Before (local) | On Azure |
|---|---|
| `kecore` CLI, outputs in `clients-local/` | Function App `fn-kecore-<prefix>-v9` on the shared B1 plan; Durable Functions: profile → fiches in parallel → report; outputs in container `kecore-<client>` |
| LLM record `clients-local/kecore/llm-cache/` | Blob, same key (`<deployment>@<host>` + request hash), so a run replays at no model cost |
| `llm.json` | App settings in Bicep + managed identity (keyless) |
| kefind in-memory index (BM25 + TF-IDF stand-in) | Azure AI Search index `idx-<client>-fiches` (indexer over the decomposed JSON, real `text-embedding-3-large` vectors); score fusion and the decision stay in code |
| kefind CLI | Inside the Web App's Diagnostic tab, in place of the guide's LOCATE step |
| Real tickets as a local jsonl | ServiceNow incidents polled by a zero-connector Logic App (API-accessible instances), or an ITSM export an operator drops in `tickets-<client>/raw/` (EasyVista clients, no API); a Function scrubs it into a Table and deletes the raw export |
| Labeling sheet (xlsx) | Web App tab gated by an Entra group (same model as `/itsm`) → Table; ServiceNow `m2m_kb_task` (KB article attached to a closed incident) pre-fills a candidate label, a human confirms |
| scoreboard CLI | Function run on demand → report in Blob |
| Pilier 2 JSON file + CLI review | Table + review tab; `observe` called on every real question |

ServiceNow, beyond ticket intake: kefind's result is written back to the
ticket as an internal work note by a separate executor Logic App with a closed
schema, only after an agent validates it (pillar 7, same rule as the ITSM
module: nothing touches a real ticket without human validation); a fiche whose
resolution is an action of the closed ITSM list hands the ticket over to the
ITSM action engine (propose → approve → execute); `kb_knowledge` can feed
`kb-<client>` for clients whose KB lives in ServiceNow.

Two choices made with the decision:
- **Parity first.** Slice 2 keeps the current text extraction (python-docx /
  pypdf) inside the Function so the uploaded LLM record replays the
  2026-10-01 run on the 242 real client-s fiches: it must reproduce exactly
  163 guided / 22 citable / 57 info_only and 2228/2228 verified steps, with no
  model call — proof the move changed nothing. Switching the source text to
  Document Intelligence markdown (the platform's "managed extraction"
  principle) comes after, as a measured before/after on the same 242 fiches.
- **AI Search for kefind.** One extra index per client. Basic tier caps a
  service at 15 indexes, i.e. about 7 clients at two indexes each.

| Slice | Content | Status |
|---|---|---|
| 1 | Bicep: Function App, `kecore-<client>` / `tickets-<client>` containers, RBAC (`infra/modules/kecore.bicep`) | ✅ Deployed 2026-10-06 — runbook §15 |
| 2 | kecore on Azure + parity test on the 242 real fiches (`kecore_func/`) | ✅ Deployed 2026-10-06; parity PASS on Azure: 242 fiches, 163 / 22 / 57, 2228 / 2228, mean agreement 0.919, 0 model calls — runbook §16 |
| 3 | Finding the fiche from entities and the graph: client dictionary on the ticket, entity index `idx-<client>-fiches`, graph between fiches; text only breaks ties | 🟢 Next |
| 4 | Tickets (ServiceNow poll on the PDI, export upload for client-s), labeling tab, scoreboard on Azure | 🟢 Planned |
| 5 | kefind in the live Diagnostic + pilier 2 loop + dictionary review tab | 🟢 Planned |
| 6 | Bridge to the ITSM action engine + work-note write-back | 🟢 Planned |
| 7 | Remove the local-writing CLIs and `clients-local/kecore` (after upload) | 🟢 Planned |

## Working rules for this track

- No code without an explicit go-ahead.
- Nothing runs on, or is written to, an operator's machine (decided
  2026-10-05): compute runs in Azure (Functions, the Web App), data lives
  in Azure Storage, infrastructure is Bicep. See "Azure-native migration".
- `git add` / `commit` / `push` are run by the repo owner from their own
  terminal only.
- A performance claim is measured on real data, never estimated, and
  compared before/after on the exact same sample.
- No mechanism that only works for one client — everything here is generic
  and automated by construction, never a per-client branch or a hard-coded
  dictionary.
