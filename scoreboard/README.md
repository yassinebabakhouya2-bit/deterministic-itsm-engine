# Scoreboard (slice 1)

Measure before building. The scoreboard replays labeled tickets through an
engine several times and reports, for each engine:

| Metric | Meaning |
| --- | --- |
| Exact fiche @1 | The fiche shown is a right one, over tickets that have one |
| Wrong fiche shown | A fiche is shown and it is wrong, over all tickets: the number kept under a ceiling |
| No fiche shown | The engine abstained or asked a question |
| Correct "no fiche" | Nothing shown on tickets that no fiche covers |
| Recall@5 | A right fiche is among the first five candidates: the ceiling for a better decision step |
| Stability | Same decision in every run |
| Latency p50 / p95, cost per ticket | From timings and the token usage the engine reports |

Every rate comes with its 95% interval. When several engines are reported
together, they are also compared on the same tickets with an exact McNemar test.
Every later slice is kept only if it moves these numbers.

## Rules

- Real tickets never leave `clients-local/` (git-ignored). Results and reports go there by default.
- Labels and engines must name fiches the same way: the ServiceNow number (`KB0012345`) when there is one,
  otherwise the document name without folder and extension.

## Workflow

From the repository root, in PowerShell:

```powershell
# 1. Tickets from the ITSM export (CSV, XLSX, JSON or JSONL); column names are matched loosely
python -m scoreboard import-tickets clients-local\exports\client-s-tickets.csv --client client-s `
  --id-col "Numéro" --text-col "Objet" --text-col "Description" `
  --out clients-local\scoreboard\client-s.tickets.jsonl

# 2. Search-baseline settings read from the live index
python -m scoreboard inspect-index --endpoint https://<search-service>.search.windows.net `
  --index idx-client-s --client client-s --out clients-local\scoreboard\search-baseline.json

# 3. Labeling sheet with five candidates per ticket (.xlsx needs openpyxl; .csv works without)
python -m scoreboard prepare-labels clients-local\scoreboard\client-s.tickets.jsonl `
  --engine search-baseline --engine-config clients-local\scoreboard\search-baseline.json `
  --out clients-local\scoreboard\client-s.labels.xlsx

# 4. In Excel, fill "expected": 1-5 (a candidate), a fiche id (several: id1|id2), or none
python -m scoreboard apply-labels clients-local\scoreboard\client-s.labels.xlsx `
  --tickets clients-local\scoreboard\client-s.tickets.jsonl `
  --out clients-local\scoreboard\client-s.labeled.jsonl

# 5. Replay, 5 runs by default
python -m scoreboard run clients-local\scoreboard\client-s.labeled.jsonl `
  --engine search-baseline --engine-config clients-local\scoreboard\search-baseline.json

# 6. Report on every engine run so far
python -m scoreboard report "clients-local\scoreboard\results\*.results.jsonl" `
  --tickets clients-local\scoreboard\client-s.labeled.jsonl --max-wrong 5%
```

Golden sets that already name the right fiche import directly with `--expected-col`
(add `--expected-transform basename` when they name files).

Without Azure, on the synthetic demo:

```powershell
python -m scoreboard run scoreboard\examples\demo.tickets.jsonl --engine fixture --engine-config scoreboard\examples\demo.standard.json
python -m scoreboard run scoreboard\examples\demo.tickets.jsonl --engine fixture --engine-config scoreboard\examples\demo.careful.json
python -m scoreboard report "clients-local\scoreboard\results\demo-*.results.jsonl" --tickets scoreboard\examples\demo.tickets.jsonl
```

## Engines

- `search-baseline`: Azure AI Search on `idx-<client>`, first fiche shown. It proposes the candidates of the
  labeling sheet and gives a first reference point. Keyless: your account needs the Search Index Data Reader
  role; the token comes from azure-identity when it is installed, otherwise from `az login`. Settings:
  `examples/search-baseline.example.json`, or let `inspect-index` write them.
- `fixture`: replays decisions from a JSON file (tests, demo).
- Any `package.module:factory`: `factory(config)` returns an object with a `name` and `decide(ticket)`,
  which returns a `Decision` or a dict with `kind` (`fiche`, `question`, `abstain`), `fiches`, `score`
  and `usage`. An optional `candidates(ticket, k)` pre-fills the labeling sheet.

Next: the KnowledgeEngine decision engine (slice 3) plugs in the same way.

## Abstention threshold

For engines that return a score, the report replays "show the fiche only when the score reaches t" for
every t, and recommends the lowest t whose wrong-fiche rate stays under the ceiling at the top of its 95%
interval. With zero wrong fiches, proving 5% takes 73 tickets and 2% takes 189: below that no threshold
can be proven, and the report says so. Choose the threshold on one set of tickets and confirm it on another.

## Costs

Default prices are the gpt-4o 2024-11-20 list prices in USD per million tokens (input 2.50, output 10.00).
Pass `--prices` with a JSON file holding your Azure prices and currency (see `scoreboard/pricing.py`).

## Tests

```powershell
python -m unittest discover -s scoreboard/tests -t .
```

No dependency is required. Optional: `openpyxl` (XLSX sheets), `azure-identity` (tokens without the Azure CLI).
