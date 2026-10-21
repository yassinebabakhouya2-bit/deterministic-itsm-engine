# Handoff: build the new clean project (read this first)

Written 2026-10-21 so that a fresh session (Cowork or Claude Code) can continue **without re-doing the analysis**.
The analysis cost a lot of tokens; do not repeat it. Work sequentially and directly, file by file. **Do not launch
multi-agent workflows** unless the owner asks for one; read large files in slices; run only the suites a change can affect.

## 1. Why a new project

The Azure subscription of tenant KnowledgeEngineV9655 was disabled (read-only, `ReadOnlyDisabledSubscription`), the deploy of
the V10 routing work was refused, and it will not be reactivated. The owner decided to rebuild the platform in a **brand-new tenant and
subscription**, in a **new GitHub repository**, with only what works and is used, no leftover code or folders, no duplicate
implementations, and documentation written for a first reader. The old data is **not** backed up or migrated (decision 2026-10-20): the
new platform starts from the sources (client documents, a fresh ticket export); labels, reference questions and dictionary decisions are
redone there.

## 2. Where things are

| What | Where |
| --- | --- |
| Old repo (source of everything) | GitHub `yassinebabakhouya2-bit/deterministic-itsm-engine`; local `C:\V9\knowledgeengine-rag-platform`. `main` = merge `9b91a20` (V10 kecore/kefind/scoreboard + deterministic routing L0-L4, all merged). Branch `claude/kecore-variantes-selection-wpu4ro` = `main` + runbook §19.21 + this handoff. |
| New repo (empty, target) | GitHub `yassinebabakhouya2-bit/deterministic-itsm-engine-platform`. Nothing pushed yet. |
| Tests of the old repo (all green at `43a5e39`) | kecore 163, kefind 356, scoreboard 144, kecore_func 295 (`python -m unittest discover -s <pkg>/tests -t .`), Web App 215 (`python -m pytest -q -p no:cacheprovider tests`). |
| Analysis results | this folder: `removal-candidates.md`, `dedup-groups.md` (+ `dedup-groups.json` full detail), `gotchas.md`, `reader-questions.md`. |
| Design of the engine | `docs/v10-deterministic-engine.md`, `kecore_func/README.md`, runbook §19.20 (routing). |

Repo rules (`CLAUDE.md`) still hold in the new repo: 100 % managed Azure services, nothing that is part of the system runs on or writes to an
operator machine, English everywhere except the French KB/eval/demo content and UI copy, every Azure operation logged in the runbook in the
same change, `git add/commit/push` run from the owner's own terminal (a Cowork/Claude session edits files and hands back the git commands).

## 3. Decisions (final)

- (a) New Entra tenant + subscription; every global Azure name derives from ONE prefix parameter.
- (b) Keep every working feature: the deterministic engine and Diagnostic, labels, dictionary review, routing review; the RAG fallback and OPEN
  answer over Azure AI Search with the enrichment Function and skillsets (documents only); SharePoint-to-blob ingestion (documents only); the ITSM
  action engine (poll/propose/execute Logic Apps, `/itsm` tab, demo seeding); the Diagnostic write-back; multi-client support including the
  synthetic clients clienta/b/c.
- (c) **Remove audio and video entirely**: `ingestion/audio-transcribe`, `ingestion/video-index`, `infra/modules/videoindexer.bicep`, Speech and
  Language PII, `audio-raw-*`/`video-raw-*` containers, transcript handling (`sourcetype` audio/video) in search/answer code, the
  `*transcribed*` scripts, `backfill-pii-pipeline.ps1`, the bootstrap audio/video phases, their docs.
- (d) Remove dead code: the `/classic` archive and `convindex`/`convturns`, the never-enabled ServiceNow webhook and `/diag/internal/sweep`
  (+ `scripts/enable-diagnostic-webhook.ps1`), the old 5-step kefind pipeline and the local CLIs (`python -m kecore|kefind|scoreboard`, they write under
  `clients-local/`, forbidden by CLAUDE.md), the local xlsx/labeling tooling, `search_baseline`, `demo/`, `infra/deploy-tenants.local.json`, one-off
  repair/migration scripts, and the tests and docs of whatever goes. Details and verifier verdicts: `removal-candidates.md`.
  **Check every removal with grep first**: the verifiers found several items still referenced (`kecore/pending.py`, `kefind/io.py`,
  `kefind/understand.py`, `scoreboard/engines/fixture.py`, `config/engine.client{a,b,c}.yaml`, `infra/modules/appsettings-merge.bicep`).
- (e) One implementation per job: merge the duplicates in `dedup-groups.md`, smallest and safest first, suites green after each batch.
- (f) Docs for a first reader: README, CLAUDE.md, architecture, an A-to-Z deployment guide (French operator blocks, like runbook §19.19/§19.20),
  an operations guide with the current procedures and only the gotchas that still apply (`gotchas.md`), **no incident log**.
- (g) Behaviour preserved. Persisted formats stay as they are: table and blob names, PartitionKey/RowKey schemes, `ticketlabels.skipped` as
  Edm.Boolean, `refquestions`, `kefindreview` keyed by (fiche_id, sha256 of the EXTRACTED text), `fiche_key = sha256(fiche_id)[:32]`,
  `taxonomy.json`/routing-table sha256 in the compact JSON form, `dictionary-decisions.json`, `exclusion-config.json`,
  `kefind/routing.py` `NORMALIZATION_VERSION`. Model record keys (`llm-cache/`, `embed-cache/`) may change: they are void in a new tenant.
- Packaging: Web App = `app/`, `orchestration/`, `config/`; kecore Function = `kecore_func/`, `kecore/`, `kefind/`, `scoreboard/`; enrichment
  Function = `enrichment/`. A package cannot import another: a shared helper used by two packages stays a tiny copy with a parity test (a shared
  folder copied by the deploy scripts is not worth a few lines).
- Open, default recommendation if the owner does not say otherwise: drop `eval/` (RAG evaluation to the local disk and Foundry; the scoreboard is
  the measurement) and the Foundry *project* resource; keep `kb/clienta|b|c` and the synthetic client configs.

## 4. Environment facts (2026-10-20/21)

- Tenant `ITSMEngine.onmicrosoft.com`, id `d6a526f1-fba8-4064-9401-e1dfaae8d5e2`. Subscription "Azure subscription 1", id
  `84d279d8-eddc-49fd-9baf-3a4815f9cdc7`, Enabled, the owner is Owner. Offer type (pay-as-you-go vs free trial) to confirm; a budget alert must be
  created (Cost Management, 100 EUR/month, alerts at 50 % and 90 %). Region `francecentral`.
- Global-name prefix: **`itsmeng`** (proposed, to confirm); no `-v9` suffix. Storage name <= 24 chars, lowercase letters and digits.
- Quota seen in francecentral: gpt-4o **Standard 50 kTPM** (the Bicep default 30 + 20 fits exactly); GlobalStandard gpt-4o = 0 (not used).
  **Not yet known**: `text-embedding-3-large` quota (default capacity 120) and whether gpt-4o `2024-11-20` can still be deployed to a new
  subscription (legacy model, retirement 2027-04-14). The owner was given a check block (providers, embedding quota, model list) and has not
  pasted its output yet. If gpt-4o 2024-11-20 is refused, the model name, version and capacity must be Bicep parameters (make them so anyway) and
  every record/measure must be redone on the new model before reactivating routing.
- Resource providers: the owner registered 10 by hand, 2 hit SSL errors (unknown which). The bootstrap must register AND verify them itself.
- The owner works in Windows PowerShell 5.1, French locale, from `C:\V9\...`. Scripts: ASCII only, stop at the first error, no `az login` pasted
  together with other commands (the login prompt swallows the next lines).
- Known SSL hiccups from `az` on the owner's machine (VPN/antivirus inspection): a retry usually works.

## 5. Work plan (sequential, ~8 steps; commit after each)

1. **Import.** Clone the new repo locally (`C:\V9\deterministic-itsm-engine-platform`). Copy the tracked files of the old repo at the branch tip
   (`git ls-files` of the old repo is the list) EXCEPT the obvious drops (`demo/`, `infra/deploy-tenants.local.json`, audio/video folders, transcribed
   scripts). One import commit, then every cleanup is a small commit so the diffs stay reviewable. Add a `.gitignore` that keeps real parameter
   files and `clients-local/` out.
2. **Remove dead code** per `removal-candidates.md` (grep every symbol and path first; update imports, tests, Bicep params, packaging lists,
   docs). Run the five suites after each area (web app, engine, infra/scripts).
3. **Remove audio/video** (decision c) across code, search skillsets, Bicep, bootstrap phases and docs.
4. **Merge duplicates** from `dedup-groups.md`. Start with `identical` + `high` groups; read the members yourself (only the `text` angle was
   verified). Keep behaviour and persisted formats (decision g). Run the suites after each batch.
5. **Naming.** One `prefix` parameter + an environment parameters file (real values git-ignored, a committed `.example`). Replace every hard-coded
   `knowledgeengine`, `-v9`, `stknowledge`, `kv-ke3`, tenant/group/subscription ids (`grep -rniE` over the new tree; known spots: `app/app.py`
   `_STORAGE_ACCOUNT`, `orchestration/answer.py` endpoints, `kecore.bicep`, `kecore-link.bicep`, `deploy-kecore-function.ps1`, `config/*.yaml`) by app
   settings fed from Bicep outputs.
6. **Infra and bootstrap.** Consolidate the Bicep (foundation, kecore Function, Web App, kecore-link, Search, ingestion, enrichment, ITSM, write-back)
   and rewrite `scripts/bootstrap-new-tenant.ps1` for a new tenant: provider registration + verification, model quota preflight
   (`az cognitiveservices usage list`), Entra objects (App Registrations, groups, roles), Key Vaults, layers in order, the deploys of the three
   packages, loading the client's documents into `kb-<client>/Kbs/` and the ticket export, first kecore run in mode `record`. `bicep build` and a
   `pwsh` parser check for every file. Mark any step you could not verify as NOT VERIFIED.
7. **Docs.** README, CLAUDE.md, architecture, deployment guide A-to-Z, operations guide + gotchas (see `gotchas.md`), engine spec.
8. **Verify and ship.** All suites green, `bicep build`, `pwsh` parse, a grep for leftover old names, one adversarial review of the whole diff;
   the owner pushes. Then deploy with the owner, logging every Azure command in the new runbook in the same change.

## 6. Next owner actions (Azure)

1. Confirm the offer type of the subscription and create the budget alert.
2. Run the verification block (providers, embedding quota, gpt-4o 2024-11-20 availability) and report the output.
3. Confirm the prefix `itsmeng` and whether the client's documents will come from a SharePoint site of the new tenant or be uploaded directly
   (default: direct upload for the first run, SharePoint ingestion as an optional layer).
