# kecore: KnowledgeEngine core (slice 2: decomposition of KB fiches)

Every fiche of a client's KB becomes a list of steps that the engine can show and follow. Each step is the
fiche's own words: a span of its text, never a model's copy or summary of it.

| Output per fiche | Meaning |
| --- | --- |
| Steps | Span of the fiche, kind (`check` observes, `action` changes something), section role, condition, "si échec" link, optional rewording |
| Entities | Error codes, event ids, fiche and update numbers, paths, registry keys, commands, URLs, menu paths, keyboard shortcuts, applications, operating systems, each with a canonical id (`err:0x80070005`, `menu:fichier > options`) |
| Status | `guided`: rules and LLM agree, the fiche can be followed step by step · `citable`: shown as a source, never guided · `info_only`: no resolution step |

## How a fiche is decomposed

1. Boilerplate lines learned from the client's KB (contact lines, legal notices) are removed; e-mail addresses and
   phone numbers are masked.
2. Two independent methods split the fiche into steps: rules (`segment.py`: headings, lists, instruction verbs)
   and the LLM (`llm_segment.py`).
3. Every LLM quote must be found in the fiche after a fixed normalization (spaces, typographic apostrophes and
   quotes, dashes, markdown marks, case); otherwise it is dropped. A rewording is kept only if it adds no command,
   path, menu, key or code that the step does not contain.
4. The two splits are aligned on their spans. Agreement of 0.8 or more gives high confidence.
5. A "si échec" link exists only when the fiche writes it ("Si le problème persiste", "En cas d'échec",
   "passez à l'étape 4").

Every LLM answer is recorded under the hash of its exact request: running again reads the record, gives the same
steps and costs nothing. `--replay` never calls the model; `--refresh` calls it again.

The writing profile is learned per client from its own fiches: section names and their roles (keyword table, then
the LLM once for unknown names), boilerplate, list style. When the profile learned on two halves of the KB
disagrees, only the keyword table is trusted.

## Rules

- Real fiches and everything derived from them stay in `clients-local/` (git-ignored): outputs and the LLM record go
  to `clients-local/kecore/` by default.
- Fiche ids follow `kecore.ids`, shared with the scoreboard: the ServiceNow number (`KB0012345`) in the file name or
  title, otherwise the file name without its extension.

## Commands

From the repository root, in PowerShell:

```powershell
# Fiches from a folder (.md, .txt, .html; .docx and .pdf need python-docx and pypdf)
python -m kecore decompose kb\clienta --client clienta

# With the LLM pass (keyless: your account needs Cognitive Services OpenAI User on the resource)
python -m kecore decompose kb\clienta --client clienta --llm-config clients-local\kecore\llm.json

# Same run again from the record, no model call
python -m kecore decompose kb\clienta --client clienta --llm-config clients-local\kecore\llm.json --replay

# Fiches from a KB export table; several text columns become sections named after them
python -m kecore import-fiches clients-local\exports\kb-client-s.csv --client client-s `
  --id-col "Référence" --title-col "Titre" --body-col "Description" --body-col "Solution" `
  --out clients-local\kecore\client-s.fiches.jsonl

# Look at one decomposed fiche
python -m kecore show clients-local\kecore\clienta\fiches.decomposed.jsonl KB0010001
```

`llm.json` follows `kecore/examples/llm.example.json`. Each run writes, in `clients-local/kecore/<client>/`:
`fiches.decomposed.jsonl`, `profile.json`, `report.md` (the gate of the slice, the fiches to look at, references
to fiches missing from the KB) and `summary.json`.

## Tests

```powershell
python -m unittest discover -s kecore/tests -t .
```

No dependency is required. Optional: `openpyxl` (XLSX exports), `python-docx` and `pypdf` (Word and PDF fiches),
`azure-identity` (tokens without the Azure CLI).
