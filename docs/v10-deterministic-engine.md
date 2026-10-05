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
not an impression.

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
| 2 | Dynamic per-client entity dictionary (replaces `kecore.entities.APPS`) | 🟡 In progress — offline corpus scan (`kecore.profile.build_dictionary`) shipped; the online `pending_synonyms` feedback loop is not started |
| 3 | Knowledge-graph relations between fiches (replaces the no-op `kefind.graph_filter`) | 🟢 Planned — needs pillar 2 and a real embedding service |
| 4 | Canonical ingestion schema | 🟢 Planned |
| 5 | Bayesian step ordering (p/C ratio) | 🟢 Planned — needs pillar 2 |
| 6 | Conformal abstention (replaces fixed `kefind.decide.Thresholds`) | 🟢 Planned — calibration must use only validated tickets, never raw production ones |
| 7 | ServiceNow write-path guardrails (Pydantic + RBAC) | 🟢 Planned |
| 8 | Event sourcing / traceability | 🟢 Planned |

Dependency order: pillar 2 first (everything else needs a real entity
dictionary); then a real embedding service (prerequisite for pillars 3 and
5); then pillars 1 and 4; then pillar 3; then pillars 5–6; then 7–8.

## Working rules for this track

- No code without an explicit go-ahead.
- `git add` / `commit` / `push` are run by the repo owner from their own
  terminal only.
- A performance claim is measured on real data, never estimated, and
  compared before/after on the exact same sample.
- No mechanism that only works for one client — everything here is generic
  and automated by construction, never a per-client branch or a hard-coded
  dictionary.
