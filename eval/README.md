# Evaluation — quantified proof of reliability

Golden datasets and evaluation runs via the **Azure AI Foundry** evaluators:
**Faithfulness**, **Groundedness**, **Relevance**.

Reliability is **measured**, not promised (axiom A5). Every change to the engine
or the index is revalidated against the golden dataset → continuous quality control.

Files:
- `golden_<client>.jsonl` — **synthetic** golden dataset per client (e.g. `golden_clienta.jsonl`): sample questions + expected answers + reference context.
- Run results (timestamped, versioned scores).

> 100% synthetic data (no real client data). One golden dataset per client, aligned with its dedicated index.
