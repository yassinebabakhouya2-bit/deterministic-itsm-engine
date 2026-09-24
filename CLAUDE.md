# CLAUDE.md — working rules for this repository

Project: KnowledgeEngine v9 — multi-client Azure RAG platform (private repo
`yassinebabakhouya2-bit/knowledgeengine-rag-platform`). This file is read by
every Claude session (Claude Code, Cowork, or any other surface) that opens
this repository. That is the point of it: it is the one enforcement
mechanism that survives across separate conversations and even separate
accounts, unlike Claude's own project memory, which is tied to one account
and one conversation.

## Mandatory: log every operation to `docs/operations-runbook.md`, live

Whenever a session running in this repo runs a command against Azure
(`az`, `curl`/`Invoke-RestMethod` against a management/Search/Speech/Graph
REST endpoint, PowerShell, Cloud Shell), deploys or redeploys anything
(Bicep, zip-deploy, indexer trigger, Logic App retrigger), or hits and
fixes a bug during that work — append it to `docs/operations-runbook.md`
**in the same turn**, before moving to the next task. Do not defer this to
"later" or to an end-of-session summary, and do not rely on memory to carry
it forward across sessions — that is exactly the failure mode this file
exists to close.

Where an entry goes:
- A one-time platform prerequisite (App Registration, Key Vault, RBAC role
  that applies once per environment, not per client) → §0 or the matching
  "one-time" subsection.
- A step in onboarding a new client (SharePoint grant + admin consent,
  ingestion deploy, Search pipeline deploy, auth/tenant isolation config,
  webapp redeploy) → the matching numbered client-onboarding section.
- A bug hit and fixed, whether or not it seems likely to recur → its own
  subsection, in the format already used throughout this file: **symptom
  → root cause → fix**, with the exact command/config that resolved it.
  Add a reusable script under `scripts/` when the fix is more than a
  one-off command, and reference it from the runbook instead of
  duplicating its body there.
- Something you had to do but couldn't fully verify or automate (e.g. a
  step still done by hand in the Azure Portal) → note it as such rather
  than dressing it up as a clean CLI recipe. A documented manual step is
  more useful than an invented command that will fail for the next person.

The runbook update is part of the change, not an afterthought: when Yassine
commits the code/infra change from his own terminal, the runbook diff
documenting it belongs in that same commit (or the very next one, for an
infra-only operation with no code change).

## Why this file, not project memory

Claude's own project memory (the workflow/roadmap/jalon-N files visible in
this account's Claude sessions) is for conversation continuity — narrative
status, decisions made, what's still open. It is invisible outside Claude
and useless to a future Claude session on a different account, or to a
human reading the repo. `docs/operations-runbook.md` is the single source
of truth for **how to reproduce, deploy, and operate this system**:
commands, exact resource names/parameters, gotchas — because it lives in
git and travels with the code. When a fact belongs in both places, it still
has to be written here; the memory copy is a pointer, never a substitute.

## Cloning or redeploying this project for a new client

`docs/operations-runbook.md` §0 through its per-client sections is the full
path from an empty Azure subscription to a working multi-client deployment;
the per-client sections repeat for each additional client. Read it end to
end before touching Azure — it encodes real incidents (RBAC propagation
delays, Graph consent traps, PowerShell encoding bugs, indexer stalls,
tenant-creation captcha bugs) that are not obvious from the Bicep/code alone
and will resurface if skipped. If you complete a step the runbook marks as
a gap (see its "not yet captured" notes), close the gap in the same session
rather than leaving it for the next person to rediscover.

## Project conventions this file does not repeat in full

- One dedicated conversation per milestone ("jalon"); Claude's own project
  memory carries cross-conversation continuity for that — see its
  `workflow.md` for the reading convention at the start of a session. This
  file is about what goes in git, not about how Claude's memory is used.
- Everything in this repo is in English (commit messages, docs, code/config
  comments) except: `kb/client{a,b,c}/*.md`, `eval/golden_client*.jsonl`,
  the `SYSTEM_PROMPT` in `eval/evaluate_rag.py`, and the `demo/` UI copy —
  all deliberately kept French, tied to already-indexed/measured content.
- 100% managed Azure services — no custom glue code/script deployed or run
  as part of the pipeline itself (Logic Apps / Data Factory / Prompt Flow /
  Foundry Evaluation only). A `scripts/*.ps1` helper run manually by an
  operator to fix data (like the ones referenced in the runbook) is not
  "the pipeline" and is fine; anything meant to run continuously or be
  deployed as part of the system is not.
- `git add`/`commit`/`push` always run from Yassine's own terminal, never
  from the Claude device bridge (`device_bash`): non-interactive credential
  prompts fail there, and even a read-only git command from that bridge has
  left a stale `.git/index.lock` before on this repo. Read and edit files
  directly instead, and hand back the exact git commands for Yassine to run
  himself.
