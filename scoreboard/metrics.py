"""Scoring rules.

Every rate carries a 95% Wilson interval: on 100 tickets the honest error bar
is several points wide, and the report says so instead of hiding it.

Definitions (on the first run, the one a technician would see):

* exact fiche @1: the fiche shown is one of the expected fiches, over tickets
  that have an expected fiche;
* wrong fiche shown: a fiche is shown and it is not an expected one, over all
  tickets (a fiche shown for a ticket that no fiche covers is wrong);
* no fiche shown: the engine abstained or asked a question, over all tickets;
* correct "no fiche": no fiche shown, over tickets that no fiche covers;
* recall@5: an expected fiche is among the first 5 candidates, whatever the
  decision (the ceiling for a better decision step);
* stability: same kind and same fiche shown in every run, over all tickets.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

Z95 = 1.959963984540054
RECALL_K = 5


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def tickets_needed(max_rate: float, z: float = Z95) -> int | None:
    """Smallest n for which zero errors out of n puts the rate under max_rate.

    With no error the upper Wilson bound is z^2 / (n + z^2), hence the closed form.
    """
    if not 0 < max_rate < 1:
        return None
    n = max(1, math.ceil(z * z * (1 - max_rate) / max_rate) - 1)
    while wilson(0, n, z)[1] > max_rate:
        n += 1
    return n


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    data = sorted(values)
    rank = (pct / 100) * (len(data) - 1)
    low, high = math.floor(rank), math.ceil(rank)
    return data[low] + (data[high] - data[low]) * (rank - low)


@dataclass(frozen=True)
class Rate:
    k: int
    n: int

    @property
    def value(self) -> float | None:
        return self.k / self.n if self.n else None

    @property
    def low(self) -> float | None:
        return wilson(self.k, self.n)[0] if self.n else None

    @property
    def high(self) -> float | None:
        return wilson(self.k, self.n)[1] if self.n else None

    def to_dict(self) -> dict:
        return {"k": self.k, "n": self.n, "value": self.value, "ci95": [self.low, self.high] if self.n else None}


def ticket_key(record: dict) -> str:
    return f"{record['client']}/{record['ticket_id']}"


def shown(record: dict) -> str | None:
    if record.get("kind") == "fiche" and record.get("fiches"):
        return record["fiches"][0]
    return None


def is_exact(record: dict) -> bool:
    fiche = shown(record)
    return fiche is not None and fiche in record["expected"]


def by_run(records: list[dict]) -> dict[int, dict[str, dict]]:
    runs: dict[int, dict[str, dict]] = defaultdict(dict)
    for record in records:
        runs[int(record["run"])][ticket_key(record)] = record
    return dict(sorted(runs.items()))


def canonical(records: list[dict]) -> list[dict]:
    runs = by_run(records)
    return list(next(iter(runs.values())).values()) if runs else []


@dataclass
class EngineSummary:
    engine: str
    runs: int
    tickets: int
    with_fiche: int
    without_fiche: int
    exact: Rate
    wrong_shown: Rate
    no_fiche: Rate
    questions: Rate
    correct_none: Rate
    recall_at_k: Rate
    stability: Rate | None
    exact_by_run: list[float | None]
    latency_p50: float | None
    latency_p95: float | None
    cost_per_ticket: float | None
    currency: str
    errors: int
    error_samples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {}
        for name, value in self.__dict__.items():
            out[name] = value.to_dict() if isinstance(value, Rate) else value
        return out


def summarize(engine: str, records: list[dict], prices=None) -> EngineSummary:
    runs = by_run(records)
    if not runs:
        raise ValueError(f"no results for engine {engine}")
    first = next(iter(runs.values()))
    canon = list(first.values())
    with_fiche = [r for r in canon if r["expected"]]
    without_fiche = [r for r in canon if not r["expected"]]
    n = len(canon)

    exact = sum(1 for r in with_fiche if is_exact(r))
    wrong = sum(1 for r in canon if shown(r) is not None and shown(r) not in r["expected"])
    no_fiche = sum(1 for r in canon if shown(r) is None)
    questions = sum(1 for r in canon if r["kind"] == "question")
    correct_none = sum(1 for r in without_fiche if shown(r) is None)
    recall = sum(1 for r in with_fiche if set(r["fiches"][:RECALL_K]) & set(r["expected"]))

    stability = None
    if len(runs) > 1:
        stable = 0
        for key, record in first.items():
            signature = (record["kind"], shown(record))
            if all(key in run and (run[key]["kind"], shown(run[key])) == signature for run in runs.values()):
                stable += 1
        stability = Rate(stable, n)

    exact_by_run: list[float | None] = []
    for run in runs.values():
        answerable = [r for r in run.values() if r["expected"]]
        exact_by_run.append(sum(1 for r in answerable if is_exact(r)) / len(answerable) if answerable else None)

    latencies = [float(r["latency_s"]) for r in records if r.get("latency_s") is not None and not r.get("error")]
    failed = [r for r in records if r.get("error")]
    cost = None
    if prices is not None and records:
        cost = sum(prices.cost(r.get("usage") or {}) for r in records) / len(records)
    samples = []
    for record in failed:
        if record["error"] not in samples:
            samples.append(record["error"])
        if len(samples) == 5:
            break

    return EngineSummary(
        engine=engine,
        runs=len(runs),
        tickets=n,
        with_fiche=len(with_fiche),
        without_fiche=len(without_fiche),
        exact=Rate(exact, len(with_fiche)),
        wrong_shown=Rate(wrong, n),
        no_fiche=Rate(no_fiche, n),
        questions=Rate(questions, n),
        correct_none=Rate(correct_none, len(without_fiche)),
        recall_at_k=Rate(recall, len(with_fiche)),
        stability=stability,
        exact_by_run=exact_by_run,
        latency_p50=percentile(latencies, 50),
        latency_p95=percentile(latencies, 95),
        cost_per_ticket=cost,
        currency=prices.currency if prices is not None else "",
        errors=len(failed),
        error_samples=samples,
    )


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar test on the discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


@dataclass
class PairComparison:
    first: str
    second: str
    tickets: int
    first_only: int
    second_only: int
    both: int
    neither: int
    p_value: float

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def compare(first: str, first_records: list[dict], second: str, second_records: list[dict]) -> PairComparison:
    """Paired comparison of exact fiche @1 on the tickets both engines saw."""
    a = {ticket_key(r): r for r in canonical(first_records) if r["expected"]}
    b = {ticket_key(r): r for r in canonical(second_records) if r["expected"]}
    keys = sorted(set(a) & set(b))
    first_only = sum(1 for k in keys if is_exact(a[k]) and not is_exact(b[k]))
    second_only = sum(1 for k in keys if is_exact(b[k]) and not is_exact(a[k]))
    both = sum(1 for k in keys if is_exact(a[k]) and is_exact(b[k]))
    return PairComparison(
        first=first,
        second=second,
        tickets=len(keys),
        first_only=first_only,
        second_only=second_only,
        both=both,
        neither=len(keys) - first_only - second_only - both,
        p_value=mcnemar_exact(first_only, second_only),
    )


@dataclass
class SweepRow:
    threshold: float | None
    exact: Rate
    wrong_shown: Rate
    no_fiche: Rate

    def to_dict(self) -> dict:
        return {
            "threshold": self.threshold,
            "exact": self.exact.to_dict(),
            "wrong_shown": self.wrong_shown.to_dict(),
            "no_fiche": self.no_fiche.to_dict(),
        }


@dataclass
class Calibration:
    max_wrong: float
    rows: list[SweepRow]
    recommended: SweepRow | None

    def to_dict(self) -> dict:
        return {
            "max_wrong": self.max_wrong,
            "recommended": self.recommended.to_dict() if self.recommended else None,
            "rows": [row.to_dict() for row in self.rows],
        }


def calibrate(records: list[dict], max_wrong: float) -> Calibration | None:
    """What if the engine showed its fiche only when its score is at least t?

    The recommended threshold is the lowest one whose wrong-fiche rate stays
    under the ceiling even at the top of its 95% interval: on a small sample
    this is deliberately strict. Pick it on one set of tickets and confirm it
    on another before trusting it.
    """
    canon = canonical(records)
    scored = [r for r in canon if shown(r) is not None and r.get("score") is not None]
    if not scored:
        return None
    n = len(canon)
    answerable = sum(1 for r in canon if r["expected"])
    unscored = [r for r in canon if shown(r) is not None and r.get("score") is None]
    rows = []
    for threshold in [None] + sorted({float(r["score"]) for r in scored}):
        kept = unscored + [r for r in scored if threshold is None or float(r["score"]) >= threshold]
        exact = sum(1 for r in kept if is_exact(r))
        rows.append(
            SweepRow(
                threshold=threshold,
                exact=Rate(exact, answerable),
                wrong_shown=Rate(len(kept) - exact, n),
                no_fiche=Rate(n - len(kept), n),
            )
        )
    recommended = next((row for row in rows if row.wrong_shown.high is not None and row.wrong_shown.high <= max_wrong), None)
    return Calibration(max_wrong=max_wrong, rows=rows, recommended=recommended)
