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
| 2 | Dynamic per-client entity dictionary (replaces `kecore.entities.APPS`) | 🟡 In progress — offline corpus scan (`kecore.profile.build_dictionary`) now also mines product names out of document names (not just trigger words), classified by one LLM call per run and verified against the corpus, with a permanent human-rejection file (`dictionary-decisions.json`, runbook §17.4). `kefind.funnel` applies the run's dictionary to the ticket. The online feedback loop is on Azure with slice 5: live questions counted once per session, hash-only storage below 3 sessions, review tab `/dictionary`, decisions read by the next run (runbook §19.2) |
| 3 | Knowledge-graph relations between fiches (replaces the no-op `kefind.graph_filter`) | ✅ Done for the deterministic track — `kefind.graph` (duplicates, number conflicts, references, prerequisites, supersession), pure code, no embeddings needed; used by `kefind.funnel` to prune candidates before ranking |
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
| 3 | Finding the fiche from entities and the graph: client dictionary on the ticket, a graph between fiches, text only breaking ties | ✅ Deployed 2026-10-07; confirmed on real client-s data (242 fiches) — runbook §17.7 |
| 4 | Tickets (ServiceNow poll on the PDI, export upload for client-s), labeling tab, scoreboard on Azure | ✅ Deployed 2026-10-08 — export scrubbed into Table Storage (every column cleaned, runbook §18.5), labeling tab `/labels`, scoreboard runs on Azure with a floor chosen on half the labels and confirmed on the other (runbook §18.6). Blank run on the 370 real tickets: 88 fiches shown, 282 questions, 0 abstentions, 0 errors (runbook §18.9). Waits on labels for its first accuracy number. The ServiceNow intake is deferred until a client's KB lives in ServiceNow (client-s is EasyVista, export only) |
| 5 | kefind in the live Diagnostic + pilier 2 loop + dictionary review tab | ✅ Deployed 2026-10-08 — on Azure, "Mon compte est bloqué, je n'arrive plus à me connecter à Windows" gives KB0120 LOCKED ACCOUNT and its 17 verified steps (runbook §19.8); the engine in front of the search index (verified steps word for word, the deciding map pinned per session, the index when the engine abstains or is down), dictionary loop and review tab `/dictionary` (runbook §19.1–19.2) |
| 6 | Bridge to the ITSM action engine + work-note write-back | 🟡 Deployed 2026-10-08 in dry run (nothing written to ServiceNow until the PDI test, runbook §19.7) — an ITSM agent validates the note (fiche and steps only), its own Logic App writes it into the incident, dry run first; the bridge is the handover (incident assigned to KE-Automation with the proposed action, picked up by `itsm/poll`) (runbook §19.3) |
| 7 | Remove the local-writing CLIs and `clients-local/kecore` (after upload) | 🟢 Planned |

The classic assistant's own free-form RAG answer (`orchestration/answer.py::diagnostic_query_core_keyless`) is now also the Diagnostic's fallback when `kefind` and the search index find no fiche (`Phase.OPEN`, runbook §19.10) — `kefind` still decides every fiche; the fallback never does.

Since 2026-10-09 the two assistants are one (runbook §19.12): the chosen fiche is shown first (title,
summary, every step) and its steps only run once the user starts them; `/classic` keeps the conversations
saved before the merge, read-only.

## The semantic mode: fiches found by meaning, thresholds calibrated on the KB (2026-10-09)

The funnel below ranks by words (BM25F). Measured offline on the 175 ranked fiches of client-s, with
no ticket: given a fiche's own first description sentence, it ranked that fiche first 96% of the
time but showed it only 38% of the time, and on the 370 real tickets 259 of its 282 questions were
ties words cannot break ("ligne" in a French question, "line" in an English fiche). An independent
review kept the skeleton (entities, graph, the code decides, verbatim steps, recorded model calls)
and replaced two components: the relevance signal and the calibration source.

- **Relevance by meaning, frozen per run.** Once per kecore run, offline, the model writes a card
  per fiche (what it solves, in French and English, and ~10 questions people ask for it; checked by
  code: no invented error code, fiche number, path, contact); every entry is embedded once
  (text-embedding-3-large, 1024 dimensions, recorded) and frozen with its sha256
  (`kefind/cards.py`, `kefind/semantic.py`). A question is embedded once (recorded: the same text
  always gets the same vector); a fiche's score is its best cosine with the question, in pure
  Python, fixed order. This replaces the `TfidfEmbeddingProvider` stand-in in the live path.
- **Code decides.** `kefind.semantic.decide`: show if the score reaches `floor` and strictly leads by
  `margin`, offer above `offer`, else abstain. Only strong entities still filter. In the Diagnostic,
  an abstention by meaning is the answer: no search index or model judge picks a fiche instead; a
  fiche decided by words during an embedding outage is offered, never shown.
- **Calibration without tickets.** An independent generation writes exam questions per fiche, never
  indexed; thresholds are chosen on half of them under a Wilson bound on wrong fiches shown and a
  leave-one-out bound on fiches shown when the right one is absent, then measured on the other half
  against targets fixed in advance (`kefind/calibrate.py`). Without a successful calibration no fiche
  is ever shown alone, and thresholds the test half contradicts on safety (wrong fiche shown, or a
  fiche shown when the right one is absent) are withheld: the engine then only offers.
- **Guarantee.** Same normalized question + same run = byte-identical decision; a replay of the run
  rebuilds the index and its calibration with zero model calls. Not guaranteed: 100% correctness on
  real tickets, or the same answer for two different phrasings.

Operations, artifacts and deployment: runbook §19.11.

## Semantic enrichment at ingestion, deterministic routing at runtime (decided 2026-10-09)

Why: the semantic mode above ranks by a score. Measured, it puts the right fiche first 46% of the
time on the KB exam, and live it shows "KB0217 - Transfert d'appels TEAMS" for "comment attribuer
une ligne teams" (the right fiche, "KB0233 - Associate a phone line", came 3rd or 4th). The engine
is deterministic -- the same question always gets the same answer -- but determinism is not
correctness. Decision (runbook §19.13): move the meaning upstream, into structured per-fiche
metadata written at ingestion, checked by code and **validated by a person**; at runtime, route
through tables, not scores. The semantic mode is frozen (no more tuning) until the benchmark below
decides whether it stays as the L3 fallback.

**Per-fiche metadata** (`kecore-<client>/runs/<run>/enrichment/<fiche_id>.json`):
`canonical_intent` (a value of the client's closed taxonomy), `primary_app` / `supported_apps`
(values of the client's application dictionary), `app_evidence` (verbatim quotes, checked with
`NormalizedText`) and `app_inferred` (no quote: allowed, but validation is mandatory),
`semantic_aliases_fr` / `semantic_aliases_en`, `trigger_keywords` (never route on their own),
`confusable_with`, `status` (`proposed` / `validated` / `rejected`), `content_sha256` (a changed
fiche goes back to review), `excluded`, `provenance` (model, request hash, prompt version).

**Extraction**, three passes, every answer recorded under its request hash (`RecordingLLM`: a replay
rewrites the same metadata with no model call -- reproducibility comes from the record, not from
`temperature=0`): A, per fiche, a strict JSON Schema proposal; B, once per client, the proposed
intents merged into a closed taxonomy, reviewed, frozen and versioned; C, each fiche assigned an
intent from that closed enum. Code checks: apps in the dictionary, quotes found verbatim, aliases
2-8 words with no invented technical entity (`novel_technical_entities`), keywords present in an
alias or the fiche. Human decisions live outside the runs (like `dictionary-decisions.json`), keyed
by (fiche, `content_sha256`), and are re-applied by the next run.

**Runtime routing**, first level that decides wins; every answer carries `route`, the rule, the
alias or key that decided, and the routing tables' sha256:

| Level | Rule | Result |
|---|---|---|
| L0 identifier | a fiche number, error or event code known to one fiche | that fiche, exact |
| L1 validated alias | every token of a **validated** alias is among the question's tokens (fixed, versioned normalization: case, accents, punctuation, frozen FR/EN stop words, a validated inflection table); the most specific alias wins | that fiche, exact |
| L2 (intent, app) | the model maps the question onto the closed (intent, app) enum (recorded under the normalized question's hash; code validates the enum); table lookup | one validated fiche: exact; several: a closed question; none: L3 |
| L3 meaning | today's semantic ranking, app filter only when exactly one app is detected and a fiche carries it | never exact: proposals |
| L4 nothing | -- | the free-form answer (`Phase.OPEN`), labelled as such |

Collisions are resolved at ingestion, never at runtime: an alias token set claimed by two fiches is
not routable at L1 and goes to review; an (intent, app) key shared by two validated fiches is either
intended (L2 asks) or fixed in review.

**System exclusion** (in kecore's `report` phase, before any index): a fiche is excluded when its
normalized title or document name contains, as whole words, a pattern of the client's list (default
`A REUTILISER`, `NE PAS UTILISER`), or when it has no verified step and under 40 characters of body (text without
boilerplate and without the fiche's own title). Every exclusion is listed with its reason in the run;
a human decision can force a fiche back in. Implemented in `kecore/exclusion.py`, applied by the run's
`report` phase (runbook §19.14). The defaults narrowed from the first draft (`LIBRE`, `A REUTILISER`,
`OBSOLETE`, `NE PAS UTILISER`, 200 characters) once measured: a bare `LIBRE` or `OBSOLETE` matches real
titles, and 200 characters excluded a real two-line policy fiche of the demo KB.

**Order and gate**: ground truth first (30-50 real technician questions with their fiche in
`/labels`, including "comment attribuer une ligne teams" -> KB0233 and questions no fiche answers);
then the exclusion rule; then passes A/B/C and the review tab; then the routing tables and L0-L2;
then the benchmark on the scoreboard, same set before and after (McNemar). Deploy only if a wrong
fiche shown as exact (L0-L2) is **0** and right-fiche-first is at least today's engine's.

## How the funnel finds a fiche (slice 3)

`kefind.funnel.find` replaces the 5-step `kefind` pipeline above for the
"which fiche" decision. Entities first, the graph next, text only to break
ties — the code decides at every step, never the LLM:

1. **Interpretation** (`kefind.interpret`, the only model call in the path):
   turns the ticket into English and French search terms, and names the
   likely application. The model proposes; the code verifies each term (at
   most 12, 6 words each, no error code / fiche number / path / command /
   URL / menu / contact detail absent from the ticket) and drops anything
   that fails. These terms never filter and never decide — they only widen
   the text-ranking step below.
2. **Entities**: `kecore.entities.extract_entities`, with the same client
   dictionary used to read the fiches. An entity absent from the map is
   ignored and traced.
3. **Filter by levels**, most informative first: a technician's own answers
   to a prior question, a cited fiche number (resolved through the graph),
   identifiers (error code, event, ServiceNow KB number), update number,
   application, technical elements (path, registry, command, URL, menu,
   shortcut). A fiche passes a level if it carries at least one of the
   ticket's entities there; it must pass every level kept. When no fiche
   passes, the least informative level is dropped and the attempt retried
   (traced). The operating system never filters — a ticket mentions it too
   often in passing — it only drives a disambiguating question.
4. **Graph** (`kefind.graph`): a duplicate becomes its canonical fiche, a
   replaced fiche the fiche replacing it.
5. **Text**: BM25F over the structure kecore extracted (title ×3, symptom
   and cause ×2, steps ×1, body ×1), with the ticket's own words and the
   interpretation's terms. Text only ranks the fiches the entities and the
   graph already kept.
6. **Decision** by thresholds: a fiche clearly ahead → shown directly; close
   fiches where only one repeats at least two words of its own title (name
   of the document included, at least one informative) → that fiche; close
   fiches an entity could tell apart → a question on that entity; no entity
   at all → a stricter text-only search across the whole map, a question
   asking the application if it comes close, otherwise abstention.

Measured on Azure (runbook §17.7, 2026-10-07), real model, real dictionary
on the 242 real client-s fiches: 3 of 4 spot-checked tickets matched a
hand-written prediction exactly on the first try (Teams cache, AutoCAD
licence by title match, an English "account locked" ticket needing no
interpretation at all); the 4th ("Mon compte est bloqué, je n'arrive plus à
me connecter à Windows") first missed — the interpretation prompt asked only
for the fix ("unlock account"), never the problem's own state ("account
locked"), so it never met "LOCKED ACCOUNT"'s title. Fixed by rephrasing the
prompt to ask for both; the same ticket then reaches
"KB0120- LOCKED ACCOUNT" directly. The client has no KB field on its tickets and is not
ready to label by hand yet, so slice 4 started reduced: scrub the real export into Table
Storage and run every ticket through the funnel once, unlabeled, to see the shape of what
comes back (fiche / question / abstain, and why) -- no accuracy number without labels, and
that part (even 20-30 labeled tickets would do) stays ahead, along with the scoreboard and
threshold calibration. Runbook §18.

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
